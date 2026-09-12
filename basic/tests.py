from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation


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

        # Create 3 jurors to vote for taker
        juror1 = User.objects.create_user(username='j1', password='p')
        juror2 = User.objects.create_user(username='j2', password='p')
        juror3 = User.objects.create_user(username='j3', password='p')
        for j in [juror1, juror2, juror3]:
            UserProfile.objects.create(user=j, rewards=100)

        dispute = Dispute.objects.get(task=self.task)
        for j in [juror1, juror2, juror3]:
            self.client.login(username=j.username, password='p')
            self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'taker', 'stake_amount': 50})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
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


class DisputeEscrowJuryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        # Give initial points to users
        for u in [self.poster, self.taker, self.juror1, self.juror2, self.juror3]:
            UserProfile.objects.get_or_create(user=u, defaults={'rewards': 1000})

        # Poster creates a task
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Test Task',
            'description': 'Task details',
            'reward': '200',
            'deadline': deadline
        })
        self.task = Task.objects.get(title='Test Task')

        # Taker takes the task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()

    def test_disputed_task_endpoint_locks(self):
        # Taker raises a dispute
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfinished work'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        # Poster attempts to complete task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        # Poster attempts to cancel task
        response = self.client.get(reverse('cancel_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        # Poster attempts to request cancellation
        response = self.client.get(reverse('request_cancellation', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_reward_ledger_escrow_lock_on_dispute(self):
        # Taker raises a dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work disputed'})
        
        escrow_entries = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_escrow_lock')
        self.assertEqual(escrow_entries.count(), 1)
        self.assertIn('escrow', escrow_entries.first().description.lower())

    def test_juror_voting_consensus_and_settlement_favor_taker(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Disputed reward'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster or Taker cannot vote on own dispute
        self.client.login(username='poster', password='password123')
        resp = self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'poster', 'stake_amount': 50})
        self.assertEqual(dispute.votes.count(), 0)

        # Juror 1 votes for Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'taker', 'stake_amount': 50})

        # Juror 2 votes for Poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'poster', 'stake_amount': 50})

        # Juror 3 votes for Taker (3rd vote triggers threshold >= 3 and consensus 2 vs 1)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'taker', 'stake_amount': 50})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'completed')

        # Check taker received payout
        payout_tx = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout')
        self.assertEqual(payout_tx.count(), 1)
        self.assertEqual(payout_tx.first().amount, 200)

        # Check winning jurors (juror1 & juror3) received juror rewards
        j1_reward = RewardLedger.objects.filter(user=self.juror1, task=self.task, transaction_type='juror_reward')
        self.assertEqual(j1_reward.count(), 1)
        self.assertGreater(j1_reward.first().amount, 0)

    def test_juror_voting_settlement_favor_poster(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # 3 Jurors vote for Poster
        for j in [self.juror1, self.juror2, self.juror3]:
            self.client.login(username=j.username, password='password123')
            self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': 'poster', 'stake_amount': 50})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winner, self.poster)
        self.assertEqual(self.task.status, 'cancelled')

        # Check poster received refund
        refund_tx = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund', amount=200)
        self.assertEqual(refund_tx.count(), 1)
        self.assertEqual(refund_tx.first().amount, 200)

    def test_rewards_view_escrow_balance(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Under review'})

        # Check rewards view
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['escrowed_dispute_balance'], 200)

