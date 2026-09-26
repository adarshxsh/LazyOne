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


class SymmetricalDisputeStakingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Poster & Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        # Task (reward=300, bond=60)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Symmetrical Test Task",
            description="Testing Symmetrical Bonds",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

    def test_poster_counter_bond_success(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'pending')

        # Poster logs in and posts counter-bond
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('post_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_escrow_status, 'held')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000 - 60)

        # Check ledger
        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_poster_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

    def test_poster_counter_bond_insufficient_rewards(self):
        self.poster_profile.rewards = 20
        self.poster_profile.save()

        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})

        dispute = Dispute.objects.get(task=self.task)

        # Poster attempts to post counter-bond with only 20 points
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('post_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_escrow_status, 'pending')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 20)

    def test_poster_counter_bond_sla_expiration_default_win(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})

        dispute = Dispute.objects.get(task=self.task)
        # Fast-forward deadline into past
        dispute.counter_bond_deadline = timezone.now() - timedelta(hours=2)
        dispute.save()

        # Check SLA trigger
        triggered = dispute.check_counter_bond_sla()
        self.assertTrue(triggered)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.consensus_outcome, 'taker')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: 200 - 60 (initial deposit) + 60 (refund) + 300 (reward) = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

    def test_juror_stake_insufficient_rewards(self):
        # Raise dispute and post counter-bond
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_counter_bond', args=[dispute.id]))

        # Juror with 10 points attempts to vote (25 required)
        self.juror1_profile.rewards = 10
        self.juror1_profile.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 10)
        from .models import JurorAssignment
        self.assertFalse(JurorAssignment.objects.filter(dispute=dispute, juror=self.juror1, has_voted=True).exists())

    def test_consensus_resolution_slashing_and_reward_distribution(self):
        # Setup active dispute with both bonds held (60 from taker, 60 from poster)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('post_counter_bond', args=[dispute.id]))

        # Juror 1 votes for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'taker'})

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 75)  # 100 - 25 stake

        # Juror 3 votes for poster (dissenting minority)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'poster'})

        # Juror 2 votes for taker -> reaches consensus (2 votes taker vs 1 poster)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'taker'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.consensus_outcome, 'taker')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker: 200 - 60 (deposit) + 60 (refund) + 300 (reward) = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        # Poster: 1000 - 60 (forfeited bond) = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        # Minority Juror 3: 100 - 25 (slashed) = 75
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 75)

        # Reward Pool = losing poster deposit (60) + slashed minority stake (25) = 85
        # Majority jurors = Juror 1 & Juror 2 (2 jurors). Per juror reward = 85 // 2 = 42.
        # Juror 1 & 2 balance: 75 + 25 (stake refund) + 42 (reward share) = 142
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 142)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 142)

        # Ledger check for Juror 1
        j1_refund_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_refund').first()
        self.assertIsNotNone(j1_refund_ledger)
        self.assertEqual(j1_refund_ledger.amount, 25)

        j1_reward_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(j1_reward_ledger)
        self.assertEqual(j1_reward_ledger.amount, 42)

        # Ledger check for Minority Juror 3 slash
        j3_slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').first()
        self.assertIsNotNone(j3_slash_ledger)


