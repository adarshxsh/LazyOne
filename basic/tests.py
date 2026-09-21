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
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

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

    def test_add_task_deducts_reward_and_poster_deposit_bond(self):
        self.client.login(username='poster', password='password123')
        future_deadline = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        # Reward 300 + Deposit 60 = 360 required
        response = self.client.post(reverse('add_task'), {
            'title': 'New Task',
            'description': 'Desc',
            'reward': '300',
            'deadline': future_deadline
        })

        self.assertRedirects(response, reverse('home'))
        self.poster_profile.refresh_from_db()
        # 1000 - 360 = 640
        self.assertEqual(self.poster_profile.rewards, 640)

        new_task = Task.objects.get(title='New Task')
        task_ledger = RewardLedger.objects.filter(user=self.poster, task=new_task, transaction_type='task_creation').first()
        self.assertIsNotNone(task_ledger)
        self.assertEqual(task_ledger.amount, -300)

        bond_ledger = RewardLedger.objects.filter(user=self.poster, task=new_task, transaction_type='poster_dispute_deposit').first()
        self.assertIsNotNone(bond_ledger)
        self.assertEqual(bond_ledger.amount, -60)

    def test_non_disputed_task_completion_refunds_poster_bond(self):
        # Poster created self.task previously. Manually complete task.
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.poster_profile.refresh_from_db()
        # 1000 + 60 (poster deposit bond refunded) = 1060
        self.assertEqual(self.poster_profile.rewards, 1060)

        refund_ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

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

        # Deposit bond is 60. Taker balance was 200 -> now 140
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.poster_deposit_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_juror_voting_and_stake_commitment(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror1 votes for taker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_option': 'taker', 'staked_amount': '50'}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 50)

        dispute.refresh_from_db()
        self.assertEqual(dispute.total_juror_stake, 50)
        self.assertEqual(dispute.status, 'voting')

        vote = DisputeVote.objects.get(dispute=dispute, voter=self.juror1)
        self.assertEqual(vote.voted_option, 'taker')
        self.assertEqual(vote.staked_amount, 50)

        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_held').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

    def test_participant_prohibited_from_juror_voting(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster attempts to vote as juror
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'voted_option': 'poster', 'staked_amount': '50'}
        )
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.poster).exists())

    def test_dispute_resolution_slashing_and_dividend_payouts(self):
        # Taker raises dispute (taker bond 60 deducted: 200 -> 140)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror1 votes 'taker' (majority, stake 50)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'taker', 'staked_amount': '50'})

        # Juror2 votes 'taker' (majority, stake 50)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'taker', 'staked_amount': '50'})

        # Juror3 votes 'poster' (minority, stake 50)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_option': 'poster', 'staked_amount': '50'})

        # Resolve dispute (Taker wins with 2 votes vs 1)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.poster_deposit_status, 'forfeited')

        # Check Taker: starting remaining 140 + task reward 300 + deposit refund 60 = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        # Check Poster: started 1000, 60 poster deposit forfeited = 1000
        # (Note: poster's deposit of 60 was deducted at raise_dispute or task creation)
        self.poster_profile.refresh_from_db()

        # Dividend Pool = Slashed minority stakes (50) + Losing party bond (60) = 110 points
        # Majority jurors total staked = 100. Each juror has 50/100 (50%).
        # Each majority juror gets: 50 stake refund + 55 dividend = 105 points added to 50 remaining = 155 total.
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 155)

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 155)

        # Minority Juror 3: 50 remaining (stake slashed, 0 refund/dividend)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 50)

        # Verify all ledger transaction types
        types_in_ledger = set(RewardLedger.objects.values_list('transaction_type', flat=True))
        self.assertIn('dispute_deposit', types_in_ledger)
        self.assertIn('juror_stake_held', types_in_ledger)
        self.assertIn('juror_stake_refunded', types_in_ledger)
        self.assertIn('juror_stake_slashed', types_in_ledger)
        self.assertIn('juror_reward_payout', types_in_ledger)

