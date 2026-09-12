from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeVote, RewardLedger

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
        refund_tx = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund')
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

