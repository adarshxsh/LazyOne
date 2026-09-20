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


class SymmetricalBondsAndJurorStakingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='task_poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=500)

        # Task taker / doer
        self.taker = User.objects.create_user(username='task_taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        # Create task: reward = 300, deposit bond = 60
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

        # Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

    def test_symmetrical_poster_deposit_on_dispute_initiation(self):
        self.client.login(username='task_taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not accepted'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.poster_deposit_status, 'held')

        # Check taker balance: 200 - 60 = 140
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Check poster balance: 500 - 60 = 440
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 440)

        # Check reward ledger entries
        taker_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(taker_ledger)
        self.assertEqual(taker_ledger.amount, -60)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='poster_dispute_deposit').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, -60)

    def test_poster_insufficient_rewards_blocks_dispute(self):
        # Set poster rewards below deposit bond amount (60)
        self.poster_profile.rewards = 30
        self.poster_profile.save()

        self.client.login(username='task_taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not accepted'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Balances remain untouched
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 200)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 30)

    def test_juror_voting_and_stake_commitment(self):
        # Raise dispute first
        self.client.login(username='task_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work issue'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Juror1 votes for taker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_option': 'taker'}
        )

        # Juror1 profile balance: 100 - 50 = 50
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 50)

        dispute.refresh_from_db()
        self.assertEqual(dispute.total_juror_stake, 50)
        self.assertEqual(dispute.status, 'voting')

        # Check juror stake held ledger
        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_held').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

    def test_juror_insufficient_balance_blocks_voting(self):
        self.client.login(username='task_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work issue'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Set juror balance to 20 (< 50)
        self.juror1_profile.rewards = 20
        self.juror1_profile.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_option': 'taker'}
        )

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 20)
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.juror1).exists())

    def test_dispute_resolution_slashing_and_dividend_payouts(self):
        # Taker raises dispute (deposit 60 deducted from taker: 200 -> 140, poster: 500 -> 440)
        self.client.login(username='task_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality dispute'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Juror1 votes 'taker' (majority)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'taker'})

        # Juror2 votes 'taker' (majority)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'taker'})

        # Juror3 votes 'poster' (minority)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'poster'})

        # Resolve dispute (Taker wins with 2 votes vs 1)
        self.client.login(username='task_taker', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.poster_deposit_status, 'forfeited')

        # Check Taker: reward 300 + deposit refund 60 + starting remaining 140 = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        # Check Poster: started with 500, lost 60 deposit = 440
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 440)

        # Check Majority Jurors (1 and 2): 50 remaining + 50 stake refund + 55 dividend = 155
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 155)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 155)

        # Check Minority Juror (3): 50 remaining (stake slashed, 0 refund)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 50)

        # Verify all 5 transaction types exist in RewardLedger
        types_in_ledger = set(RewardLedger.objects.values_list('transaction_type', flat=True))
        self.assertIn('poster_dispute_deposit', types_in_ledger)
        self.assertIn('juror_stake_held', types_in_ledger)
        self.assertIn('juror_stake_refunded', types_in_ledger)
        self.assertIn('juror_stake_slashed', types_in_ledger)
        self.assertIn('juror_reward_payout', types_in_ledger)


