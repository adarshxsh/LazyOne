from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorVote


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

    def test_resolve_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Resolve dispute in favor of taker
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner_id': self.taker.id})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
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

class DisputeEscrowAndJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create task poster, taker, and neutral jurors
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})[0]
        self.taker_profile = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})[0]
        self.juror1_profile = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 100})[0]
        self.juror2_profile = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 100})[0]
        self.juror3_profile = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 100})[0]

        # Create a task in progress and reserve reward points
        deadline = timezone.now() + timedelta(days=2)
        self.poster_profile.rewards -= 100
        self.poster_profile.save()

        self.task = Task.objects.create(
            title="Design Logo",
            description="Create a logo",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: Design Logo"
        )

    def test_complete_task_on_disputed_task_returns_permission_error(self):
        # Create dispute and set task to disputed
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed submission')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        # Check response is 403 Permission Denied
        self.assertEqual(response.status_code, 403)

        # Confirm task remains disputed and taker points unchanged
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1000)

    def test_raising_dispute_creates_escrow_lock_transaction(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task details disputed'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        # Check RewardLedger contains escrow_lock transaction
        escrow_entry = RewardLedger.objects.filter(
            task=self.task,
            transaction_type='escrow_lock'
        ).first()
        self.assertIsNotNone(escrow_entry)
        self.assertEqual(escrow_entry.user, self.poster)

    def test_task_parties_prohibited_from_voting_as_jurors(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason', quorum=3)
        self.task.status = 'disputed'
        self.task.save()

        # Poster attempt
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})
        self.assertEqual(response.status_code, 403)

        # Taker attempt
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertEqual(response.status_code, 403)

    def test_juror_voting_and_quorum_resolution_taker_wins(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Unfair rejection', quorum=3, incentive_pool=30)
        self.task.status = 'disputed'
        self.task.save()

        # Juror 1 votes for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        # Juror 2 votes for taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open') # Still 2/3 votes

        # Juror 3 votes for poster (3rd vote reaches quorum=3)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Dispute automatically resolved
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker received task reward (100)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1100)

        # Majority jurors (juror1, juror2) received juror rewards (30 / 2 = 15 points each)
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()
        self.juror3_profile.refresh_from_db()

        self.assertEqual(self.juror1_profile.rewards, 115)
        self.assertEqual(self.juror2_profile.rewards, 115)
        self.assertEqual(self.juror3_profile.rewards, 100) # Minority juror received nothing

        # Ledger transaction check
        types = list(RewardLedger.objects.filter(task=self.task).values_list('transaction_type', flat=True))
        self.assertIn('dispute_payout', types)
        self.assertIn('juror_reward', types)

    def test_juror_voting_and_quorum_resolution_poster_wins(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Work incomplete', quorum=3, incentive_pool=30)
        self.task.status = 'disputed'
        self.task.save()

        # 3 Jurors vote for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster received refund (100)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # All 3 winning jurors received rewards (30 / 3 = 10 each)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 110)

        types = list(RewardLedger.objects.filter(task=self.task).values_list('transaction_type', flat=True))
        self.assertIn('dispute_refund', types)
        self.assertIn('juror_reward', types)
