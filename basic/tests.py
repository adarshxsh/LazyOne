from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote


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


class SymmetricalCounterBondAndJurorStakingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

        # Task reward = 300, deposit bond = 60
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_poster_counter_bond_success(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unclear requirements'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.counter_bond_status, 'pending')
        self.assertIsNotNone(dispute.counter_bond_deadline)

        # Poster posts counter-bond (60 points)
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('post_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.counter_bond_status, 'posted')
        self.assertEqual(dispute.poster_deposit_amount, 60)

        # Check poster profile balance: 1000 - 60 = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_unmatched_dispute_timeout(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unclear requirements'})

        dispute = Dispute.objects.get(task=self.task)
        # Fast forward deadline to the past
        dispute.counter_bond_deadline = timezone.now() - timedelta(minutes=1)
        dispute.save()

        # Trigger timeout check via dispute_detail_view
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        dispute.refresh_from_db()

        self.assertEqual(dispute.counter_bond_status, 'expired')
        self.assertEqual(dispute.status, 'resolved')

        # Taker profile balance: taker started with 500, raised dispute (-60 = 440), refunded bond (+60) + task reward (+300) = 800
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 800)

        # Task completed
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

    def test_juror_voting_restrictions(self):
        # Setup dispute with counter bond posted
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Issue',
            deposit_amount=60,
            poster_deposit_amount=60,
            counter_bond_status='posted',
            status='open'
        )

        # Poster trying to vote as juror on own dispute
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.poster).exists())

        # Taker trying to vote as juror on own dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.taker).exists())

        # Third-party juror with insufficient funds
        self.juror1_profile.rewards = 10
        self.juror1_profile.save()
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.juror1).exists())

        # Third-party juror with sufficient funds
        self.juror2_profile.rewards = 100
        self.juror2_profile.save()
        self.client.login(username='juror2', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})
        self.assertTrue(DisputeVote.objects.filter(dispute=dispute, voter=self.juror2).exists())
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 75)  # 100 - 25

    def test_multi_party_settlement_and_slashing(self):
        # Dispute setup with counter bond
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Issue',
            deposit_amount=60,
            poster_deposit_amount=60,
            counter_bond_status='posted',
            status='open'
        )
        self.poster_profile.rewards = 940
        self.poster_profile.save()
        self.taker_profile.rewards = 440
        self.taker_profile.save()

        # Juror 1 & Juror 2 vote for worker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})

        # Juror 3 votes for poster
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        # Resolve dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Worker (taker) won:
        # Initial 440 + deposit refund (60) + task reward (300) + poster forfeited counter-bond (60) = 860
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 860)

        # Poster lost: rewards stay 940 (already deducted 60 counter-bond)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Juror 3 (minority) lost: rewards stay 75 (25 staked and slashed)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 75)
        self.assertTrue(RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').exists())

        # Jurors 1 & 2 (majority) won:
        # Started with 100, staked 25 -> 75. Slashed pool = 25 pts. Share = 25 // 2 = 12 pts. Total payout = 25 + 12 = 37 pts.
        # Balance = 75 + 37 = 112 pts.
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 112)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 112)

        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror2, transaction_type='juror_reward').exists())


