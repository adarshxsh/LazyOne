from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, Friendship, RewardLedger, Conversation, JuryAssignment, Notification
from basic.views.dispute import draw_dispute_jurors, release_juror_stakes, get_excluded_juror_user_ids


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

        # Create candidate jurors so juror drawing succeeds on raise dispute
        for i in range(3):
            u = User.objects.create_user(username=f'juror_candidate_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000)

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
        # Create task poster and taker
        self.poster = User.objects.create_user(username='poster', password='password')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        # Create task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timezone.timedelta(days=1)
        )

    def test_litigants_and_friends_exclusion(self):
        # Create direct friend of poster
        friend_poster = User.objects.create_user(username='friend_poster', password='password')
        friend_poster_profile = UserProfile.objects.create(user=friend_poster, rewards=1500)
        self.poster_profile.friends.add(friend_poster_profile)

        # Create explicit Friendship for taker (from_user)
        friend_taker_1 = User.objects.create_user(username='friend_taker_1', password='password')
        friend_taker_1_profile = UserProfile.objects.create(user=friend_taker_1, rewards=1500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=friend_taker_1_profile)

        # Create explicit Friendship for taker (to_user)
        friend_taker_2 = User.objects.create_user(username='friend_taker_2', password='password')
        friend_taker_2_profile = UserProfile.objects.create(user=friend_taker_2, rewards=1500)
        Friendship.objects.create(from_user=friend_taker_2_profile, to_user=self.taker_profile)

        # Create neutral candidate
        neutral_user = User.objects.create_user(username='neutral', password='password')
        UserProfile.objects.create(user=neutral_user, rewards=1500)

        excluded_ids = get_excluded_juror_user_ids(self.task)

        # Check that litigants and all friends are in excluded_ids
        self.assertIn(self.poster.id, excluded_ids)
        self.assertIn(self.taker.id, excluded_ids)
        self.assertIn(friend_poster.id, excluded_ids)
        self.assertIn(friend_taker_1.id, excluded_ids)
        self.assertIn(friend_taker_2.id, excluded_ids)

        # Check that neutral_user is not excluded
        self.assertNotIn(neutral_user.id, excluded_ids)

    def test_insufficient_reward_balance_exclusion(self):
        # Create candidates with low balance
        low_balance_user = User.objects.create_user(username='low_balance', password='password')
        UserProfile.objects.create(user=low_balance_user, rewards=50)

        high_balance_user = User.objects.create_user(username='high_balance', password='password')
        UserProfile.objects.create(user=high_balance_user, rewards=100)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        excluded_ids = get_excluded_juror_user_ids(self.task)

        candidates = User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=100
        ).exclude(id__in=excluded_ids)

        candidate_ids = list(candidates.values_list('id', flat=True))
        self.assertNotIn(low_balance_user.id, candidate_ids)
        self.assertIn(high_balance_user.id, candidate_ids)

    def test_successful_juror_draw_and_stake_lock(self):
        # Create 3 eligible neutral candidates
        jurors = []
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)
            jurors.append(j)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        success, selected = draw_dispute_jurors(dispute)

        self.assertTrue(success)
        self.assertEqual(len(selected), 3)

        # Verify profile rewards deducted
        for j in jurors:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 900)

            # Verify RewardLedger record created
            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_lock').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -100)

            # Verify JuryAssignment created
            assignment = JuryAssignment.objects.filter(dispute=dispute, juror=j).first()
            self.assertIsNotNone(assignment)
            self.assertEqual(assignment.staked_amount, 100)
            self.assertEqual(assignment.stake_status, 'locked')
            self.assertTrue(assignment.is_staked)

    def test_insufficient_jurors_fallback_and_admin_notification(self):
        admin = User.objects.create_user(username='admin', password='password', is_staff=True)
        # Create only 2 eligible neutral candidates
        for i in range(2):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        success, selected = draw_dispute_jurors(dispute)

        self.assertFalse(success)
        dispute.refresh_from_db()
        self.assertIn(dispute.status, ['pending_juror', 'pending-juror'])

        # Verify admin notification
        notification = Notification.objects.filter(recipient=admin).first()
        self.assertIsNotNone(notification)
        self.assertIn('pending juror draw', notification.message)

    def test_stake_release_on_dispute_resolution(self):
        # Setup 3 jurors
        jurors = []
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)
            jurors.append(j)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        draw_dispute_jurors(dispute)

        # Now release stakes
        release_juror_stakes(dispute)

        for j in jurors:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 1000)

            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_release').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, 100)

            assignment = JuryAssignment.objects.get(dispute=dispute, juror=j)
            self.assertEqual(assignment.stake_status, 'released')
            self.assertFalse(assignment.is_staked)

    def test_raise_dispute_and_withdraw_dispute_flow(self):
        # Create 3 neutral candidates
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)

        self.client.force_login(self.taker)
        response = self.client.post(f'/task/dispute/{self.task.id}/', {'reason': 'Quality issue'})
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.jury_assignments.count(), 3)

        # Check assigned jurors had stake locked
        for assignment in dispute.jury_assignments.all():
            self.assertEqual(assignment.stake_status, 'locked')
            self.assertTrue(assignment.is_staked)
            self.assertEqual(assignment.juror.userprofile.rewards, 900)

        # Withdraw dispute
        response = self.client.post(f'/dispute/withdraw/{dispute.id}/')
        self.assertEqual(response.status_code, 302)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')

        # Verify stakes refunded
        for assignment in JuryAssignment.objects.filter(dispute_id=dispute.id):
            self.assertEqual(assignment.stake_status, 'released')
            self.assertFalse(assignment.is_staked)
            self.assertEqual(assignment.juror.userprofile.rewards, 1000)
