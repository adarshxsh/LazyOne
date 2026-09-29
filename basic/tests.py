from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SymmetricalBondTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_sym', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_sym', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.task = Task.objects.create(
            title="Symmetrical Bond Task",
            description="Testing dual bonding",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_poster_counter_staking_and_timeout_sla(self):
        # Taker raises dispute -> deposit 60 pts
        self.client.login(username='taker_sym', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.poster_deposit_amount, 0)
        self.assertFalse(dispute.is_fully_backed)

        # Fast forward time past 48 hours
        dispute.counter_bond_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Access dispute detail page triggers auto SLA resolution
        self.client.get(reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker profile gets 500 - 60 (bond) + 60 (bond refund) + 300 (task reward) = 800
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 800)

    def test_poster_counter_stakes_success(self):
        # Taker raises dispute
        self.client.login(username='taker_sym', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Poster deposits matching counter bond
        self.client.login(username='poster_sym', password='password123')
        response = self.client.post(reverse('pay_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertTrue(dispute.is_fully_backed)
        self.assertEqual(dispute.status, 'voting')

        # Poster balance: 1000 - 60 = 940
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940)

        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='poster_counter_bond').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_poster_raises_dispute_and_worker_timeout(self):
        # Poster raises dispute -> deposit 60 pts
        self.client.login(username='poster_sym', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.worker_deposit_amount, 0)
        self.assertFalse(dispute.is_fully_backed)

        # Fast forward time past 48 hours
        dispute.counter_bond_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Access dispute detail page triggers auto SLA resolution
        self.client.get(reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Poster profile gets 1000 - 60 (bond) + 60 (bond refund) + 300 (task reward refund) = 1300
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

    def test_counter_bond_insufficient_rewards(self):
        # Taker raises dispute
        self.client.login(username='taker_sym', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Set poster rewards to 30 (< 60 required)
        self.poster_profile.rewards = 30
        self.poster_profile.save()

        self.client.login(username='poster_sym', password='password123')
        response = self.client.post(reverse('pay_counter_bond', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertFalse(dispute.is_fully_backed)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JurorStakingAndResolutionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_juror', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_juror', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

        self.task = Task.objects.create(
            title="Juror Staking Task",
            description="Testing juror staking and pro-rata rewards",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

        # Setup backed dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair dispute",
            status='voting',
            deposit_amount=60,
            worker_deposit_amount=60,
            worker_escrow_status='held',
            poster_deposit_amount=60,
            poster_escrow_status='held',
            counter_bond_deadline=timezone.now() + timedelta(hours=48),
            voting_deadline=timezone.now() + timedelta(hours=72)
        )
        # Taker paid 60, poster paid 60
        self.taker_profile.rewards = 440
        self.taker_profile.save()
        self.poster_profile.rewards = 940
        self.poster_profile.save()

    def test_unbacked_dispute_blocks_juror_voting(self):
        # Create unbacked dispute
        task2 = Task.objects.create(
            title="Unbacked Task", description="desc", reward=200,
            posted_by=self.poster, taken_by=self.taker, status='in_progress'
        )
        unbacked_dispute = Dispute.objects.create(
            task=task2, raised_by=self.taker, reason="Reason",
            status='open', worker_deposit_amount=50, poster_deposit_amount=0
        )

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[unbacked_dispute.id]),
            {'vote': 'worker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[unbacked_dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=unbacked_dispute).exists())

    def test_juror_voting_and_pro_rata_rewards_majority_resolution(self):
        # Juror1 votes worker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 80) # 100 - 20 stake

        # Juror2 votes worker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 80)

        # Juror3 votes poster
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'poster'})
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 80)

        # Fast forward past voting deadline and trigger resolution
        self.dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Worker wins! Taker rewards: 440 + 300 (reward) + 60 (bond refund) + 60 (forfeited poster bond) = 860
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 860)

        # Majority jurors (Juror1 & Juror2):
        # Slashed minority stakes = 20 pts (Juror3).
        # Pro-rata reward per winning juror = floor(20 / 2) = 10 pts.
        # Total payout per winning juror = 20 (stake refund) + 10 (reward) = 30 pts.
        # New balance: 80 + 30 = 110 pts.
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 110)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 110)

        # Minority juror (Juror3): slashed (remains 80 pts)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 80)

        # Check ledger entries for juror rewards and slashing
        j1_refund_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_refund').first()
        self.assertIsNotNone(j1_refund_ledger)
        self.assertEqual(j1_refund_ledger.amount, 20)

        j1_reward_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(j1_reward_ledger)
        self.assertEqual(j1_reward_ledger.amount, 10)

        j3_slash_ledger = RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').first()
        self.assertIsNotNone(j3_slash_ledger)

    def test_voting_tie_refunds_bonds_and_stakes(self):
        juror4 = User.objects.create_user(username='juror4', password='password123')
        UserProfile.objects.create(user=juror4, rewards=100)

        # 2 votes worker, 2 votes poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'poster'})

        self.client.login(username='juror4', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'poster'})

        # Fast forward and trigger resolution
        self.dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Taker balance restored: 440 + 60 = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        # Poster balance restored: 940 + 60 = 1000
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Jurors balances restored: 80 + 20 = 100
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 100)

    def test_quorum_check_under_3_votes_refunds(self):
        # Only 2 votes
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})

        # Fast forward voting deadline
        self.dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Insufficient quorum -> both deposit bonds & juror stakes refunded
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 100)

    def test_participant_cannot_vote_on_own_dispute(self):
        # Poster attempts to vote
        self.client.login(username='poster_juror', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Taker attempts to vote
        self.client.login(username='taker_juror', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_juror_insufficient_rewards_for_stake(self):
        # Set juror1 rewards to 10 (< 20 required)
        self.juror1_profile.rewards = 10
        self.juror1_profile.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.juror1).exists())

    def test_duplicate_juror_vote_prevented(self):
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'worker'})
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.juror1).count(), 1)

        # Attempt second vote
        response = self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.juror1).count(), 1)


