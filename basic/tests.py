from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryPanel, JurorVote, Friendship


class JuryPanelTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Litigants
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Friend of poster
        self.friend = User.objects.create_user(username='friend_user', password='password123')
        self.friend_profile = UserProfile.objects.create(user=self.friend, rewards=500)
        self.poster_profile.friends.add(self.friend_profile)

        # Neutral potential jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        # Task
        self.task = Task.objects.create(
            title="Disputed Jury Task",
            description="Details",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_jury_panel_auto_creation_and_exclusion(self):
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work claimed'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury_panel'))
        panel = dispute.jury_panel

        self.assertEqual(panel.status, 'voting')
        assigned_jurors = panel.jurors.all()
        self.assertEqual(assigned_jurors.count(), 3)

        # Ensure counterparties and friends are excluded
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.friend, assigned_jurors)

        # Ensure neutral jurors were assigned
        self.assertIn(self.juror1, assigned_jurors)
        self.assertIn(self.juror2, assigned_jurors)
        self.assertIn(self.juror3, assigned_jurors)

    def test_hidden_active_vote_counts_before_voting(self):
        # Raise dispute
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Evidence details',
            deposit_amount=50,
            escrow_status='held'
        )
        panel = JuryPanel.objects.create(dispute=dispute, status='voting')
        panel.jurors.set([self.juror1, self.juror2, self.juror3])

        # Assigned juror before voting -> show_vote_counts is False
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['show_vote_counts'])

        # Cast vote as juror1
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'taker'})

        # Assigned juror after voting -> show_vote_counts is True
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertTrue(response.context['show_vote_counts'])

    def test_majority_voting_consensus_settlement(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker completed work',
            deposit_amount=50,
            escrow_status='held'
        )
        self.taker_profile.rewards = 450  # 500 - 50 deposit bond
        self.taker_profile.save()

        panel = JuryPanel.objects.create(dispute=dispute, status='voting')
        panel.jurors.set([self.juror1, self.juror2, self.juror3])

        # Juror 1 votes for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'taker'})

        # Juror 2 votes for poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Juror 3 votes for taker -> majority reached (2 taker vs 1 poster)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'taker'})

        panel.refresh_from_db()
        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(panel.status, 'resolved')
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker profile: 450 + 200 (task reward) + 50 (deposit bond refund) = 700
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        # Participating jurors receive 10 reward points
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 510)
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 510)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 510)

    def test_unauthorized_user_access_restricted(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Private dispute',
            deposit_amount=50
        )
        panel = JuryPanel.objects.create(dispute=dispute)
        panel.jurors.set([self.juror1, self.juror2])

        unauthorized_user = User.objects.create_user(username='stranger', password='password123')
        self.client.login(username='stranger', password='password123')

        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))



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

