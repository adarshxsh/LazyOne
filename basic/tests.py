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


from basic.models import JurorVote
from basic.views.dispute import settle_dispute_voting_outcome
from django.core.management import call_command

class SymmetricBondingAndJurorSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        # Task: reward = 200, bond = max(50, 20% of 200) = 50
        self.task = Task.objects.create(
            title="Symmetric Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_poster_counter_bond_flow(self):
        # 1. Worker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with task'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.worker_deposit_amount, 50)
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.poster_deposit_amount, 0)
        self.assertEqual(dispute.poster_escrow_status, 'pending')
        self.assertIsNotNone(dispute.counter_bond_deadline)

        # Taker balance deducted by 50 -> 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        # 2. Poster posts matching counter bond
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('post_dispute_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting')
        self.assertEqual(dispute.poster_deposit_amount, 50)
        self.assertEqual(dispute.poster_escrow_status, 'held')
        self.assertIsNotNone(dispute.voting_deadline)

        # Poster balance deducted by 50 -> 950
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 950)

        # Ledger check for poster counter bond
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_poster_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

    def test_poster_counter_bond_timeout_worker_wins(self):
        # Worker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with task'})

        dispute = Dispute.objects.get(task=self.task)
        # Fast forward past counter-bond deadline
        dispute.counter_bond_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Run resolution command
        call_command('resolve_expired_disputes')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.consensus_outcome, 'worker_wins')
        self.assertEqual(dispute.worker_escrow_status, 'refunded')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Worker receives deposit refund (50) + task reward (200) -> 450 + 250 = 700
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

    def test_juror_voting_and_stake_locking(self):
        # Setup dispute in voting state
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_dispute_counter_bond', args=[dispute.id]))

        # Juror 1 votes for worker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Juror 1 balance deducted by 50 -> 450
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450)

        vote1 = JurorVote.objects.get(dispute=dispute, juror=self.juror1)
        self.assertEqual(vote1.vote, 'worker')
        self.assertEqual(vote1.stake_amount, 50)

        # Participant cannot vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(JurorVote.objects.filter(dispute=dispute, juror=self.poster).exists())

        # Duplicate vote rejected
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})
        self.assertEqual(JurorVote.objects.filter(dispute=dispute, juror=self.juror1).count(), 1)

    def test_settlement_worker_wins_and_slashing(self):
        # Total system initial points before task creation was reserved
        initial_total_points = (
            self.poster_profile.rewards +
            self.taker_profile.rewards +
            self.juror1_profile.rewards +
            self.juror2_profile.rewards +
            self.juror3_profile.rewards
        ) # 1000 + 500 + 500 + 500 + 500 = 3000

        self.poster_profile.rewards -= 200
        self.poster_profile.save()

        # Step 1: Worker raises dispute (50 pts)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Step 2: Poster counter bonds (50 pts)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_dispute_counter_bond', args=[dispute.id]))

        # Step 3: Jurors vote
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Step 4: Settlement
        settle_dispute_voting_outcome(dispute)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.consensus_outcome, 'worker_wins')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 750)

        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 450)
        v3 = JurorVote.objects.get(dispute=dispute, juror=self.juror3)
        self.assertTrue(v3.is_slashed)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 550)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 550)

        final_total_points = (
            self.poster_profile.rewards +
            self.taker_profile.rewards +
            self.juror1_profile.rewards +
            self.juror2_profile.rewards +
            self.juror3_profile.rewards
        ) # 750 + 700 + 550 + 550 + 450 = 3000
        self.assertEqual(final_total_points, 3000)

    def test_settlement_poster_wins_and_slashing(self):
        self.poster_profile.rewards -= 200
        self.poster_profile.save()

        # Step 1: Worker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Step 2: Poster counter bonds
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_dispute_counter_bond', args=[dispute.id]))

        # Step 3: Jurors 1 and 2 vote poster, Juror 3 votes worker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})

        # Step 4: Settlement
        settle_dispute_voting_outcome(dispute)

        dispute.refresh_from_db()
        self.assertEqual(dispute.consensus_outcome, 'poster_wins')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 550)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 550)

        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 450)

        final_total = (
            self.poster_profile.rewards +
            self.taker_profile.rewards +
            self.juror1_profile.rewards +
            self.juror2_profile.rewards +
            self.juror3_profile.rewards
        ) # 1000 + 450 + 550 + 550 + 450 = 3000
        self.assertEqual(final_total, 3000)

    def test_settlement_tie_fallback(self):
        self.poster_profile.rewards -= 200
        self.poster_profile.save()

        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_dispute_counter_bond', args=[dispute.id]))

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        settle_dispute_voting_outcome(dispute)

        dispute.refresh_from_db()
        self.assertEqual(dispute.consensus_outcome, 'tie')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 500)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 500)

        final_total = (
            self.poster_profile.rewards +
            self.taker_profile.rewards +
            self.juror1_profile.rewards +
            self.juror2_profile.rewards +
            self.juror3_profile.rewards
        ) # 1000 + 500 + 500 + 500 + 500 = 3000
        self.assertEqual(final_total, 3000)


