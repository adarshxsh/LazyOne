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


class SymmetricalStakingAndJurorSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=200)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=200)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=200)

        # Task reward 300 -> deposit bond 60
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Symmetrical Dispute Task",
            description="Testing Symmetrical Staking",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_poster_counter_stake_success(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.poster_escrow_status, 'pending')
        self.assertFalse(dispute.is_jury_review_active)

        # Poster counter-stakes
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'held')
        self.assertTrue(dispute.is_jury_review_active)

        # Poster balance: 1000 - 60 = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_poster_counter_stake_insufficient_rewards(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})

        dispute = Dispute.objects.get(task=self.task)

        # Set poster rewards to 30 (< 60 required)
        self.poster_profile.rewards = 30
        self.poster_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_escrow_status, 'pending')

    def test_jury_review_blocked_until_both_parties_counter_stake(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror tries to vote before poster counter-stakes
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.votes.count(), 0)

        # Juror balance remains 200
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 200)

    def test_juror_voting_and_stake_holding(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))

        # Juror 1 votes for Taker (stake bond 50)
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertEqual(dispute.votes.count(), 1)
        vote = dispute.votes.first()
        self.assertEqual(vote.voter, self.juror1)
        self.assertEqual(vote.voted_for, self.taker)
        self.assertEqual(vote.stake_amount, 50)
        self.assertEqual(vote.status, 'held')

        # Juror 1 rewards: 200 - 50 = 150
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 150)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

    def test_juror_insufficient_rewards_cannot_vote(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))

        # Set juror1 rewards to 20 (< 50 required)
        self.juror1_profile.rewards = 20
        self.juror1_profile.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.votes.count(), 0)

    def test_settlement_slashes_minority_and_redistributes_to_majority_and_winner(self):
        # Setup active dispute with both party deposit bonds (60 each)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))

        # Balances after deposits:
        # Taker: 500 - 60 = 440
        # Poster: 1000 - 60 = 940

        # Juror 1 & Juror 2 vote for Taker (Worker)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        # Juror 3 votes for Poster (Minority juror)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        # Settle dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('settle_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Winner is Taker (Worker): 2 votes vs 1 vote.
        # Taker receives:
        # - Original deposit bond refund: 60
        # - Compensation from losing poster bond: 60
        # - Task reward: 300
        # Total taker balance: 440 + 60 + 60 + 300 = 860
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 860)

        # Poster deposit bond was forfeited (0 returned out of 60 deducted) -> balance stays 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Minority juror 3 stake (50) is slashed! Slashed pool = 50. Balance remains 200 - 50 = 150.
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 150)
        slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').first()
        self.assertIsNotNone(slash_ledger)

        # Majority jurors 1 & 2:
        # Each gets 50 stake refunded + (50 // 2 = 25) bonus share.
        # Balance: 150 + 50 + 25 = 225
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 225)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 225)

        # Check ledger entries for majority juror
        j1_refund = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_refund').first()
        self.assertIsNotNone(j1_refund)
        self.assertEqual(j1_refund.amount, 50)

        j1_reward = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(j1_reward)
        self.assertEqual(j1_reward.amount, 25)

    def test_withdraw_dispute_refunds_both_bonds_and_juror_stakes(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('counter_stake_dispute', args=[dispute.id]))

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        # Taker withdraws dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        # All balances restored:
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 200)

