from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
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

        # Community Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

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
        self.assertEqual(self.task.deposit_bond_amount, 60)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
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

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_symmetrical_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60.
        # Taker balance: 100 -> 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster balance: 1000 -> 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'held')
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger entries for both poster and worker
        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='poster_dispute_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

        worker_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='worker_dispute_deposit').first()
        self.assertIsNotNone(worker_ledger)
        self.assertEqual(worker_ledger.amount, -60)

    def test_withdraw_dispute_symmetrical_refund(self):
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
        self.assertEqual(dispute.poster_escrow_status, 'refunded')
        self.assertEqual(dispute.worker_escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Poster balance restored: 940 + 60 = 1000
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Check refund ledger entries
        taker_refund = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(taker_refund)
        self.assertEqual(taker_refund.amount, 60)

        poster_refund = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(poster_refund)
        self.assertEqual(poster_refund.amount, 60)

    def test_juror_stake_lock_and_voting(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes for poster
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Juror 1 balance deducted by 50 (500 -> 450)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450)

        # Check juror_stake_lock ledger entry
        stake_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_lock').first()
        self.assertIsNotNone(stake_ledger)
        self.assertEqual(stake_ledger.amount, -50)

        # Check JurorVote created
        vote = JurorVote.objects.get(dispute=dispute, juror=self.juror1)
        self.assertEqual(vote.vote, 'poster')
        self.assertEqual(vote.staked_amount, 50)

    def test_juror_insufficient_rewards_cannot_vote(self):
        self.juror1_profile.rewards = 20
        self.juror1_profile.save()

        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 20)
        self.assertFalse(JurorVote.objects.filter(dispute=dispute, juror=self.juror1).exists())

    def test_task_participant_cannot_vote_as_juror(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(JurorVote.objects.filter(dispute=dispute, juror=self.taker).exists())

    def test_dispute_settlement_and_payout(self):
        # Raise dispute (both deposit 60 points)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes poster (locks 50 points -> balance 450)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Juror 2 votes poster (locks 50 points -> balance 450)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Juror 3 votes worker (locks 50 points -> balance 450)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})

        # Resolve dispute in favor of poster
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Check Poster balance:
        # Poster initial = 1000. Deposit = -60 -> 940.
        # Winner poster gets deposit bond refund (+60) + task reward refund for cancelled task (+300) = 1300.
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

        # Check Worker balance:
        # Taker initial = 100. Deposit = -60 -> 40. Deposit bond forfeited (no refund) -> balance remains 40.
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Forfeited worker bond = 60. Slashed minority (Juror 3) stake = 50. Total Pool = 110.
        # Majority jurors = Juror 1, Juror 2 (M = 2).
        # Share per majority juror = floor(110 / 2) = 55.
        # Each majority juror gets: 50 (stake refund) + 55 (reward share) = 105 payout.
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450 + 105) # 555

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 450 + 105) # 555

        # Minority Juror 3 lost locked stake: balance remains 450.
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 450)

        # Check minority slash ledger entry
        slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_vote_slash').first()
        self.assertIsNotNone(slash_ledger)

        # Check majority reward ledger entries
        juror1_reward = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(juror1_reward)
        self.assertEqual(juror1_reward.amount, 105)

    def test_sla_expired_dispute_symmetrical_refunds(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Backdate dispute created_at to 10 days ago
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        # Execute SLA command
        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.poster_escrow_status, 'refunded')
        self.assertEqual(dispute.worker_escrow_status, 'refunded')

        # Taker deposit refunded (40 + 60 = 100) + task reward completed (300) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Poster deposit refunded (940 + 60 = 1000)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Juror 1 stake refunded (450 + 50 = 500)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 500)
