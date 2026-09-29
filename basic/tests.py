from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
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

    def test_raise_dispute_counterparty_insufficient_rewards(self):
        # Poster has insufficient rewards (30 < 60 required)
        self.poster_profile.rewards = 30
        self.poster_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Taker balance unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

    def test_raise_dispute_symmetrical_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance: 100 -> 40. Poster balance: 1000 -> 940
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.taker_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'held')
        self.assertEqual(dispute.taker_escrow_status, 'held')

        # Check ledger entries for both poster and taker
        taker_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_deposit').first()

        self.assertIsNotNone(taker_ledger)
        self.assertEqual(taker_ledger.amount, -60)
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

    def test_withdraw_dispute_refunds_both_parties(self):
        # Taker raises dispute
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
        self.assertEqual(dispute.taker_escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Balances restored: Taker 40 + 60 = 100, Poster 940 + 60 = 1000
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Check refund ledger entries for both
        taker_refund = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        poster_refund = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(taker_refund)
        self.assertEqual(taker_refund.amount, 60)
        self.assertIsNotNone(poster_refund)
        self.assertEqual(poster_refund.amount, 60)

    def test_juror_micro_staking_validation_and_voting(self):
        # Open dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # 1. Poster attempts to vote -> blocked
        self.client.login(username='poster', password='password123')
        res1 = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.poster).exists())

        # 2. Juror with insufficient rewards attempts to vote -> blocked
        poor_juror = User.objects.create_user(username='poor_juror', password='password123')
        UserProfile.objects.create(user=poor_juror, rewards=5) # 5 < 10 required
        self.client.login(username='poor_juror', password='password123')
        res2 = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(res2, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=poor_juror).exists())

        # 3. Eligible juror votes successfully
        self.client.login(username='juror1', password='password123')
        res3 = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(res3, reverse('dispute_detail', args=[dispute.id]))

        # Juror rewards deducted: 100 - 10 = 90
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 90)

        vote = DisputeVote.objects.get(dispute=dispute, voter=self.juror1)
        self.assertEqual(vote.vote, 'poster')
        self.assertEqual(vote.staked_amount, 10)

        # Ledger record created
        stake_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake').first()
        self.assertIsNotNone(stake_ledger)
        self.assertEqual(stake_ledger.amount, -10)

        # 4. Duplicate vote attempt -> blocked
        res4 = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taker'})
        self.assertRedirects(res4, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, voter=self.juror1).count(), 1)

    def test_dispute_resolution_slashing_and_reward_redistribution(self):
        # Open dispute (deposit bond = 60 each)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 and Juror 2 vote for poster (majority)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        # Juror 3 votes for taker (minority)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taker'})

        # Staff resolves dispute in favor of poster
        staff = User.objects.create_superuser(username='staff', email='s@test.com', password='password123')
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Task cancelled and poster awarded reward refund (300) + deposit refund (60)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        # Initial 1000 - 60 (deposit) + 300 (task reward) + 60 (deposit refund) = 1300
        self.assertEqual(self.poster_profile.rewards, 1300)

        # Taker forfeits bond (remains at 40)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Minority Juror 3: micro-stake slashed (remains at 90)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 90)
        slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').first()
        self.assertIsNotNone(slash_ledger)

        # Majority Jurors 1 & 2:
        # Stake refund = 10, Reward share = floor((60 losing bond + 10 slashed stake) / 2) = 35.
        # Total per majority juror = 90 + 10 (refund) + 35 (reward) = 135
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 135)
        self.assertEqual(self.juror2_profile.rewards, 135)

        # Ledger checks for majority jurors
        refund_ledger1 = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_refund').first()
        reward_ledger1 = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(refund_ledger1)
        self.assertEqual(refund_ledger1.amount, 10)
        self.assertIsNotNone(reward_ledger1)
        self.assertEqual(reward_ledger1.amount, 35)

    def test_expired_dispute_refunds_all_juror_stakes(self):
        # Open dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 90)

        # Simulate expiration
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        # Run expired disputes resolution command
        call_command('resolve_expired_disputes', days=7)

        # Juror 1 stake refunded in full (90 + 10 = 100)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 100)

        juror_refund = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_refund').first()
        self.assertIsNotNone(juror_refund)
        self.assertEqual(juror_refund.amount, 10)
