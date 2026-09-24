from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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
        self.assertEqual(dispute.status, 'pending_counter_deposit')
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


class CounterDepositAndStakedJurorsProtocolTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.worker = User.objects.create_user(username='worker', password='password123')
        self.worker_profile = UserProfile.objects.create(user=self.worker, rewards=1000)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        # Task: reward = 200 -> bond = max(50, ceil(200 * 0.20)) = 50
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Protocol Test Task",
            description="Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.worker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_registered_transaction_types(self):
        types = [t[0] for t in RewardLedger.TRANSACTION_TYPES]
        self.assertIn('counter_dispute_deposit', types)
        self.assertIn('juror_stake', types)
        self.assertIn('juror_slash', types)
        self.assertIn('juror_reward', types)

    def test_poster_can_also_raise_dispute(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Worker did not submit work'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'pending_counter_deposit')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.initiator_deposit_amount, 50)
        self.assertEqual(dispute.counter_deposit_amount, 0)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 950)

    def test_post_counter_deposit_success(self):
        # Worker raises dispute
        self.client.login(username='worker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster posts counter deposit
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('post_counter_deposit', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'active_voting')
        self.assertEqual(dispute.counter_deposit_amount, 50)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 950)

        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='counter_dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

    def test_default_resolution_after_24_hours(self):
        # Worker raises dispute
        self.client.login(username='worker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Fast forward time past 24 hours
        dispute.created_at = timezone.now() - timedelta(hours=25)
        dispute.save()

        # Access dispute detail view to trigger check
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Worker (initiator) gets deposit bond refunded (50) + task reward (200)
        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rewards, 1200)

    def test_staked_juror_voting_restrictions(self):
        from .models import DisputeVote
        # Setup active voting dispute
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Testing voting',
            status='active_voting',
            initiator_deposit_amount=50,
            counter_deposit_amount=50,
            deposit_amount=100
        )

        # Poster cannot vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.worker.id})
        self.assertFalse(DisputeVote.objects.filter(voter=self.poster).exists())

        # Worker cannot vote
        self.client.login(username='worker', password='password123')
        response = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.poster.id})
        self.assertFalse(DisputeVote.objects.filter(voter=self.worker).exists())

        # Community juror can vote
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.worker.id})
        self.assertTrue(DisputeVote.objects.filter(voter=self.juror1, voted_for=self.worker).exists())
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450)

        # Duplicate vote rejected
        response = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.poster.id})
        self.assertEqual(DisputeVote.objects.filter(voter=self.juror1).count(), 1)

    def test_full_resolution_slashing_and_pro_rata_payouts(self):
        # Worker raises dispute (-50 -> worker rewards 950)
        self.worker_profile.rewards = 1000
        self.worker_profile.save()
        self.client.login(username='worker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster counter deposits (-50 -> poster rewards 950)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_counter_deposit', args=[dispute.id]))

        # Juror 1 & Juror 2 vote for worker (majority)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.worker.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.worker.id})

        # Juror 3 votes for poster (minority - 50 points slashed)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'voted_for': self.poster.id})

        # Resolve dispute
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('resolve_dispute', args=[dispute.id]))

        # Worker wins dispute!
        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rewards, 1250)

        # Poster rewards remain 950
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 950)

        # Juror 3 (minority) gets slashed -> stays 450 (lost 50)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 450)
        slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').first()
        self.assertIsNotNone(slash_ledger)

        # Juror 1 & 2 balance: 450 + 75 = 525
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 525)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 525)

        reward_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(reward_ledger)
        self.assertEqual(reward_ledger.amount, 75)

