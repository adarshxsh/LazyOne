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

        # Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

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
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.poster_escrow_status, 'none')
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
            worker_deposit_amount=60,
            worker_escrow_status='held',
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Check forfeit ledger for taker
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)

    def test_symmetrical_dispute_staking_and_contesting(self):
        # 1. Taker raises dispute (deposit bond = 60)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with work'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.poster_escrow_status, 'none')

        # 2. Poster contests dispute (posts matching deposit bond = 60)
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('contest_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'held')
        self.assertTrue(dispute.is_contested())

        # Poster rewards: 1000 - 60 = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

    def test_juror_voting_and_stake_deduction(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with work'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes for worker (stakes 20 points)
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'worker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 80) # 100 - 20

        vote = DisputeVote.objects.get(dispute=dispute, voter=self.juror1)
        self.assertEqual(vote.voted_for, self.taker)
        self.assertEqual(vote.stake_amount, 20)

        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -20)

    def test_juror_voting_insufficient_points(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with work'})
        dispute = Dispute.objects.get(task=self.task)

        self.juror1_profile.rewards = 10 # less than 20
        self.juror1_profile.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'worker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.juror1).exists())
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 10)

    def test_dispute_settlement_and_reward_redistribution_point_conservation(self):
        # 1. Setup: Taker raises dispute (deposit 60), Poster contests (deposit 60)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with work'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('contest_dispute', args=[dispute.id]))

        # 2. Juror votes:
        # Juror 1 votes for worker (stake 20)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'worker'})

        # Juror 2 votes for worker (stake 20)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'worker'})

        # Juror 3 votes for poster (stake 20)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'poster'})

        # 3. Settle dispute in favor of Worker
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[dispute.id]), {'winner': 'worker'})

        # Check results
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Balances calculation:
        # Worker: initial 100 - 60 (bond) + 60 (bond refund) + 30 (50% counterparty bond) + 300 (task reward) = 430
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 430)

        # Poster: initial 1000 - 60 (bond) + 0 (forfeited) = 940 (task reward of 300 was reserved at task creation)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Slashed pool: remaining counterparty bond (30) + minority juror 3 slashed stake (20) = 50
        # 2 majority jurors (Juror 1 & Juror 2) split 50 -> 25 points bonus each.
        # Juror 1: initial 100 - 20 + 20 (stake refund) + 25 (reward) = 125
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 125)

        # Juror 2: 125
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 125)

        # Juror 3 (minority voter): initial 100 - 20 (slashed) = 80
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 80)

        # Verify ledger entries created
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_forfeit').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').exists())


