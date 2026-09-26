from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote
from .views.dispute import resolve_dispute


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
        self.assertTrue(dispute.worker_deposited)
        self.assertFalse(dispute.poster_deposited)

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

    def test_bilateral_deposit_flow(self):
        # 1. Taker raises dispute (deposit 60 pts)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unpaid task completion'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(dispute.worker_deposited)
        self.assertFalse(dispute.poster_deposited)

        # Taker balance deducted: 100 - 60 = 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # 2. Poster deposits matching bond (deposit 60 pts)
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertTrue(dispute.poster_deposited)
        self.assertTrue(dispute.worker_deposited)

        # Poster balance deducted: 1000 - 60 = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Verify ledger entries
        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='poster_dispute_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

    def test_counter_bond_insufficient_rewards(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster balance reduced to 30 (< 60 required)
        self.poster_profile.rewards = 30
        self.poster_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertFalse(dispute.poster_deposited)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 30)

    def test_counter_bond_default_win_after_24h(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unpaid task'})
        dispute = Dispute.objects.get(task=self.task)

        # Fast forward past 24 hours
        dispute.counter_bond_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Run management command
        call_command('resolve_expired_disputes')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker profile: 40 + 60 (bond refund) + 300 (task reward) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Default win ledger entry
        default_win_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_default_win').first()
        self.assertIsNotNone(default_win_ledger)
        self.assertEqual(default_win_ledger.amount, 300)

    def test_juror_minimum_reward_requirement_and_voting(self):
        # Setup bilateral dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))

        # Juror 1 with low points (< 50)
        poor_juror = User.objects.create_user(username='poor_juror', password='password123')
        UserProfile.objects.create(user=poor_juror, rewards=30)

        self.client.login(username='poor_juror', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(voter=poor_juror).exists())

        # Juror 2 with enough points (100 >= 50)
        good_juror = User.objects.create_user(username='good_juror', password='password123')
        good_profile = UserProfile.objects.create(user=good_juror, rewards=100)

        self.client.login(username='good_juror', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertTrue(DisputeVote.objects.filter(voter=good_juror).exists())
        good_profile.refresh_from_db()
        self.assertEqual(good_profile.rewards, 50)

        lock_ledger = RewardLedger.objects.filter(user=good_juror, transaction_type='juror_stake_lock').first()
        self.assertIsNotNone(lock_ledger)
        self.assertEqual(lock_ledger.amount, -50)

    def test_self_voting_prevention(self):
        # Setup bilateral dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))

        # Taker attempts to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(voter=self.taker).exists())

        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.poster.id})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(voter=self.poster).exists())

    def test_withdraw_dispute_refunds_both_bonds(self):
        # Setup bilateral dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))

        # Taker withdraws dispute before voting starts
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        # Both profiles refunded in full
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

    def test_withdraw_dispute_blocked_after_voting_starts(self):
        # Setup bilateral dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))

        # Juror votes
        juror = User.objects.create_user(username='juror', password='password123')
        UserProfile.objects.create(user=juror, rewards=100)
        self.client.login(username='juror', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})

        # Taker attempts to withdraw
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

    def test_binary_slashing_and_reward_redistribution(self):
        # Setup task: reward = 300, deposit bond = 60
        # Taker raises dispute (deposit 60 -> Taker balance = 40)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster matches counter-bond (deposit 60 -> Poster balance = 940)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('deposit_counter_bond', args=[dispute.id]))

        # Create 3 jurors
        j1 = User.objects.create_user(username='j1', password='password123')
        j1_p = UserProfile.objects.create(user=j1, rewards=100)

        j2 = User.objects.create_user(username='j2', password='password123')
        j2_p = UserProfile.objects.create(user=j2, rewards=100)

        j3 = User.objects.create_user(username='j3', password='password123')
        j3_p = UserProfile.objects.create(user=j3, rewards=100)

        # J1 and J2 vote for Taker (Worker)
        self.client.login(username='j1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='j2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.taker.id})

        # J3 votes for Poster
        self.client.login(username='j3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'voted_for': self.poster.id})

        # Resolve dispute
        resolve_dispute(dispute)

        # Tally and math verification:
        # Winner = Taker (2 vs 1 votes).
        # Loser = Poster. Losing bond = 60.
        # Minority jurors = J3 (1 * 50 = 50 pts).
        # Total Slashed Pool = 60 + 50 = 110.
        # Winner (Taker) gets:
        # - Deposit bond refund = 60
        # - 30% dividend cut = floor(110 * 0.30) = 33
        # - Task reward = 300
        # Total Taker rewards = 40 + 60 + 33 + 300 = 433
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 433)

        # Poster lost -> rewards stay 940 (60 bond slashed)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Majority jurors (J1, J2):
        # 70% pool = 110 - 33 = 77.
        # Share per majority juror = 77 // 2 = 38.
        # J1 rewards = 50 (after lock) + 50 (stake returned) + 38 (share) = 138.
        j1_p.refresh_from_db()
        self.assertEqual(j1_p.rewards, 138)

        j2_p.refresh_from_db()
        self.assertEqual(j2_p.rewards, 138)

        # Minority juror (J3):
        # Stake 50 was locked (100 -> 50). Stake 100% slashed -> balance remains 50.
        j3_p.refresh_from_db()
        self.assertEqual(j3_p.rewards, 50)

        # Check RewardLedger entries
        self.assertTrue(RewardLedger.objects.filter(user=j3, transaction_type='juror_slash').exists())
        self.assertTrue(RewardLedger.objects.filter(user=j1, transaction_type='juror_reward').exists())
