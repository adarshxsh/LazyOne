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

    def test_raise_dispute_poster_insufficient_rewards(self):
        # Set poster rewards to 20 (less than 60 required)
        self.poster_profile.rewards = 20
        self.poster_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balances should remain unchanged
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 20)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40. Poster balance was 1000 -> now 940.
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger entries for both taker and poster
        taker_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(taker_ledger)
        self.assertEqual(taker_ledger.amount, -60)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

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

        # Taker balance restored: 40 + 60 = 100. Poster balance restored: 940 + 60 = 1000.
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from taker 100 -> 40 and poster 1000 -> 940)
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

        # Poster balance: 940 + 60 (deposit refund) = 1000
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_juror_stake_and_voting(self):
        # 1. Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # 2. Create community juror
        juror = User.objects.create_user(username='juror1', password='password123')
        juror_profile = UserProfile.objects.create(user=juror, rewards=100)

        # 3. Juror votes for worker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'worker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Juror profile rewards reduced by 10 points stake
        juror_profile.refresh_from_db()
        self.assertEqual(juror_profile.rewards, 90)

        vote = DisputeVote.objects.get(dispute=dispute, voter=juror)
        self.assertEqual(vote.vote, 'worker')
        self.assertEqual(vote.stake_amount, 10)

        ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_stake').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -10)

    def test_direct_participant_cannot_vote_as_juror(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Taker attempts to vote as juror
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'worker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.taker).exists())

    def test_dispute_resolution_distributes_juror_rewards(self):
        # 1. Raise dispute (bond = 60). Taker: 40, Poster: 940
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # 2. Two winning jurors (vote worker) and one losing juror (vote poster)
        j1 = User.objects.create_user(username='j1', password='password123')
        j1_p = UserProfile.objects.create(user=j1, rewards=100)
        j2 = User.objects.create_user(username='j2', password='password123')
        j2_p = UserProfile.objects.create(user=j2, rewards=100)
        j3 = User.objects.create_user(username='j3', password='password123')
        j3_p = UserProfile.objects.create(user=j3, rewards=100)

        # j1 and j2 vote worker (stake 10 each -> 90 balance)
        self.client.login(username='j1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})
        self.client.login(username='j2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'worker'})

        # j3 votes poster (stake 10 -> 90 balance)
        self.client.login(username='j3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        # 3. Resolve dispute in favor of worker
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[dispute.id]),
            {'winner': 'worker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Worker wins: receives deposit refund (60) + task reward (300) -> 40 + 60 + 300 = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Poster loses: forfeited bond (60). Balance remains 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Losing party bond (60) split equally between 2 winning jurors -> 30 each
        # Winning jurors get original stake (10) + 30 = 40 -> 90 + 40 = 130 rewards balance
        j1_p.refresh_from_db()
        self.assertEqual(j1_p.rewards, 130)
        j2_p.refresh_from_db()
        self.assertEqual(j2_p.rewards, 130)

        # Losing juror gets 0 refund -> balance remains 90
        j3_p.refresh_from_db()
        self.assertEqual(j3_p.rewards, 90)

