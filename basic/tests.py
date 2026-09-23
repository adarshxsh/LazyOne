from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification, JuryPanel, JuryMember


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


class JurySelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Poster and Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create Friends for Poster and Taker
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Create task
        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Testing jury selection",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    def test_jury_selection_creates_panel_and_members(self):
        # Create 3 neutral eligible candidates
        candidate1 = User.objects.create_user(username='candidate1', password='password123')
        UserProfile.objects.create(user=candidate1, rewards=150)

        candidate2 = User.objects.create_user(username='candidate2', password='password123')
        UserProfile.objects.create(user=candidate2, rewards=200)

        candidate3 = User.objects.create_user(username='candidate3', password='password123')
        UserProfile.objects.create(user=candidate3, rewards=100)

        # Raise dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not as expected'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury_panel'))
        panel = dispute.jury_panel
        self.assertEqual(panel.status, 'assigned')

        members = list(panel.members.all())
        self.assertEqual(len(members), 3)

        member_users = {m.user for m in members}

        # Check poster, taker, poster_friend, taker_friend are EXCLUDED
        self.assertNotIn(self.poster, member_users)
        self.assertNotIn(self.taker, member_users)
        self.assertNotIn(self.poster_friend, member_users)
        self.assertNotIn(self.taker_friend, member_users)

        # Check candidate1, candidate2, candidate3 are INCLUDED
        self.assertIn(candidate1, member_users)
        self.assertIn(candidate2, member_users)
        self.assertIn(candidate3, member_users)

        # Check Notifications created for jurors
        for juror in [candidate1, candidate2, candidate3]:
            notif = Notification.objects.filter(recipient=juror).first()
            self.assertIsNotNone(notif)
            self.assertEqual(notif.link, reverse('dispute_detail', args=[dispute.id]))

    def test_jury_selection_filters_low_rewards_and_inactive(self):
        # Eligible candidate
        c_eligible1 = User.objects.create_user(username='c_eligible1', password='password123')
        UserProfile.objects.create(user=c_eligible1, rewards=100)

        c_eligible2 = User.objects.create_user(username='c_eligible2', password='password123')
        UserProfile.objects.create(user=c_eligible2, rewards=500)

        c_eligible3 = User.objects.create_user(username='c_eligible3', password='password123')
        UserProfile.objects.create(user=c_eligible3, rewards=120)

        # Low rewards candidate (< 100)
        c_low_reward = User.objects.create_user(username='c_low_reward', password='password123')
        UserProfile.objects.create(user=c_low_reward, rewards=99)

        # Inactive candidate
        c_inactive = User.objects.create_user(username='c_inactive', password='password123', is_active=False)
        UserProfile.objects.create(user=c_inactive, rewards=500)

        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        member_users = {m.user for m in dispute.jury_panel.members.all()}

        self.assertNotIn(c_low_reward, member_users)
        self.assertNotIn(c_inactive, member_users)
        self.assertEqual(len(member_users), 3)

    def test_fallback_to_staff_when_insufficient_candidates(self):
        # Only 2 eligible candidates (fewer than required panel size 3)
        c1 = User.objects.create_user(username='c1', password='password123')
        UserProfile.objects.create(user=c1, rewards=200)

        c2 = User.objects.create_user(username='c2', password='password123')
        UserProfile.objects.create(user=c2, rewards=200)

        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury_panel'))
        panel = dispute.jury_panel
        self.assertEqual(panel.status, 'fallback')
        self.assertEqual(panel.members.count(), 0)

    def test_juror_and_third_party_access_control(self):
        cand1 = User.objects.create_user(username='cand1', password='password123')
        UserProfile.objects.create(user=cand1, rewards=100)

        cand2 = User.objects.create_user(username='cand2', password='password123')
        UserProfile.objects.create(user=cand2, rewards=100)

        cand3 = User.objects.create_user(username='cand3', password='password123')
        UserProfile.objects.create(user=cand3, rewards=100)

        third_party = User.objects.create_user(username='third_party', password='password123')
        UserProfile.objects.create(user=third_party, rewards=0)

        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Task dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [m.user for m in dispute.jury_panel.members.all()]
        self.assertEqual(len(assigned_jurors), 3)

        # Juror access test
        juror = assigned_jurors[0]
        self.client.login(username=juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Third-party unassigned user access test
        self.client.login(username='third_party', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))


