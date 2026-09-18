from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from basic.models import (
    UserProfile, Task, Dispute, JuryAssignment, RewardLedger,
    FriendRequest, Friendship, Notification, Conversation
)
from basic.views.dispute import select_and_stake_jurors


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Create task: reward = 300, 20% = 60 (> 50 minimum)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create small reward task: reward = 100, 20% = 20 (min 50 applies)
        self.small_task = Task.objects.create(
            title="Small Task",
            description="Small Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.small_task)

    def test_deposit_bond_calculation(self):
        # 20% of 300 = 60 (> 50)
        self.assertEqual(self.task.deposit_bond_amount, 60)
        # 20% of 100 = 20 (< 50, so minimum 50 applies)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
        # Set taker rewards to 30 (less than 60 required)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balance should remain unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        # First raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 40 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JurorSelectionAndStakeLockTests(TestCase):

    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)

        self.task = Task.objects.create(
            title='Test Task For Dispute',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        self.juror1_profile, _ = UserProfile.objects.get_or_create(user=self.juror1)
        self.juror1_profile.rewards = 1500
        self.juror1_profile.save()

        self.juror2_profile, _ = UserProfile.objects.get_or_create(user=self.juror2)
        self.juror2_profile.rewards = 1500
        self.juror2_profile.save()

        self.juror3_profile, _ = UserProfile.objects.get_or_create(user=self.juror3)
        self.juror3_profile.rewards = 1500
        self.juror3_profile.save()

    def test_automated_juror_selection_on_dispute_creation(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfinished task'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        dispute = self.task.dispute
        assignments = JuryAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_users = {a.user for a in assignments}
        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)

    def test_social_graph_exclusion(self):
        self.poster_profile.friends.add(self.juror1_profile)
        self.juror1_profile.friends.add(self.poster_profile)

        FriendRequest.objects.create(from_user=self.taker, to_user=self.juror2, is_accepted=False)

        juror4 = User.objects.create_user(username='juror4', password='password123')
        juror5 = User.objects.create_user(username='juror5', password='password123')
        p4, _ = UserProfile.objects.get_or_create(user=juror4)
        p4.rewards = 1500
        p4.save()
        p5, _ = UserProfile.objects.get_or_create(user=juror5)
        p5.rewards = 1500
        p5.save()

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        selected_count = select_and_stake_jurors(dispute, panel_size=3)

        assignments = JuryAssignment.objects.filter(dispute=dispute)
        assigned_users = {a.user for a in assignments}

        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)
        self.assertNotIn(self.juror1, assigned_users)
        self.assertNotIn(self.juror2, assigned_users)
        self.assertIn(self.juror3, assigned_users)

    def test_reward_balance_threshold_exclusion(self):
        self.juror1_profile.rewards = 50
        self.juror1_profile.save()

        juror4 = User.objects.create_user(username='juror4', password='password123')
        p4, _ = UserProfile.objects.get_or_create(user=juror4)
        p4.rewards = 1500
        p4.save()

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        select_and_stake_jurors(dispute, panel_size=3)

        assignments = JuryAssignment.objects.filter(dispute=dispute)
        assigned_users = {a.user for a in assignments}

        self.assertNotIn(self.juror1, assigned_users)

    def test_stake_locking_and_ledger_records(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        select_and_stake_jurors(dispute, panel_size=1)

        assignment = JuryAssignment.objects.get(dispute=dispute)
        juror = assignment.user

        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, 1400)
        self.assertEqual(assignment.staked_amount, 100)

        ledger_entry = RewardLedger.objects.filter(
            user=juror,
            task=self.task,
            transaction_type='juror_stake'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -100)

    def test_juror_notifications(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        select_and_stake_jurors(dispute, panel_size=1)

        assignment = JuryAssignment.objects.get(dispute=dispute)
        juror = assignment.user

        notification = Notification.objects.filter(recipient=juror).first()
        self.assertIsNotNone(notification)
        self.assertIn("selected as a juror", notification.message)
        self.assertIn(reverse('dispute_detail', args=[dispute.id]), notification.link)

    def test_insufficient_candidate_pool_audit_and_staff_notification(self):
        staff = User.objects.create_user(username='staffuser', password='password123', is_staff=True)

        for juror_prof in [self.juror1_profile, self.juror2_profile, self.juror3_profile]:
            juror_prof.rewards = 10
            juror_prof.save()

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        selected_count = select_and_stake_jurors(dispute, panel_size=3)

        self.assertEqual(selected_count, 0)

        notification = Notification.objects.filter(recipient=staff).first()
        self.assertIsNotNone(notification)
        self.assertIn("Audit Alert", notification.message)

    def test_dispute_withdrawal_releases_juror_stakes(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfinished task'})

        dispute = Dispute.objects.get(task=self.task)
        assignments = list(JuryAssignment.objects.filter(dispute=dispute))
        self.assertEqual(len(assignments), 3)

        for a in assignments:
            a.user.userprofile.refresh_from_db()
            self.assertEqual(a.user.userprofile.rewards, 1400)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

        for a in assignments:
            a.user.userprofile.refresh_from_db()
            self.assertEqual(a.user.userprofile.rewards, 1500)
            a.refresh_from_db()
            self.assertEqual(a.status, 'released')

            release_ledger = RewardLedger.objects.filter(
                user=a.user,
                transaction_type='juror_release'
            ).first()
            self.assertIsNotNone(release_ledger)
            self.assertEqual(release_ledger.amount, 100)

    def test_dispute_detail_and_chat_permissions(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        self.task.status = 'disputed'
        self.task.save()

        select_and_stake_jurors(dispute, panel_size=1)
        assignment = JuryAssignment.objects.get(dispute=dispute)
        juror = assignment.user

        other_user = User.objects.create_user(username='otheruser', password='password123')
        UserProfile.objects.get_or_create(user=other_user)

        # Assigned juror can view dispute detail
        self.client.login(username=juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Assigned juror can view chat
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)

        # Unrelated user cannot view dispute detail
        self.client.login(username='otheruser', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]), follow=False)
        self.assertEqual(response.status_code, 302)

        # Unrelated user cannot view chat
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]), follow=False)
        self.assertEqual(response.status_code, 302)
