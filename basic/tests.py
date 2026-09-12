from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation

class DisputeSettlementTestCase(TestCase):
    def setUp(self):
        # Create users & userprofiles
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.third_party = User.objects.create_user(username='thirdparty', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)
        self.third_party_profile = UserProfile.objects.create(user=self.third_party, rewards=500)

        # Create task with reward = 100
        self.reward = 100
        self.poster_profile.rewards -= self.reward  # Simulate points reserved on creation
        self.poster_profile.save()

        self.task = Task.objects.create(
            title='Test Task',
            description='Task for testing dispute split',
            reward=self.reward,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )

        # Create dispute raised by doer
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Partial work completed, disagreement on payout'
        )

        # Create Conversation for task
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.doer)

        self.poster_client = Client()
        self.poster_client.login(username='poster', password='password123')

        self.doer_client = Client()
        self.doer_client.login(username='doer', password='password123')

        self.third_party_client = Client()
        self.third_party_client.login(username='thirdparty', password='password123')

    def test_model_fields_and_ledger_types(self):
        # Verify model defaults
        self.assertIsNone(self.dispute.offered_doer_amount)
        self.assertIsNone(self.dispute.offered_by)
        self.assertIsNone(self.dispute.resolved_doer_amount)
        self.assertIsNone(self.dispute.resolved_poster_amount)
        self.assertIsNone(self.dispute.resolution_type)

        # Verify transaction types
        tx_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('dispute_payout', tx_types)
        self.assertIn('dispute_refund', tx_types)

    def test_propose_settlement_valid(self):
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.poster_client.post(url, {'offered_doer_amount': 60})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.offered_doer_amount, 60)
        self.assertEqual(self.dispute.offered_by, self.poster)
        self.assertEqual(self.dispute.offered_poster_amount, 40)

        # Check notification sent to counterparty (doer)
        notification = Notification.objects.filter(recipient=self.doer).first()
        self.assertIsNotNone(notification)
        self.assertIn('60 points', notification.message)

    def test_propose_settlement_counter_offer(self):
        # Poster offers 30
        url = reverse('propose_settlement', args=[self.dispute.id])
        self.poster_client.post(url, {'offered_doer_amount': 30})

        # Doer counters with 80
        self.doer_client.post(url, {'offered_doer_amount': 80})

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.offered_doer_amount, 80)
        self.assertEqual(self.dispute.offered_by, self.doer)
        self.assertEqual(self.dispute.offered_poster_amount, 20)

    def test_propose_settlement_invalid_amounts(self):
        url = reverse('propose_settlement', args=[self.dispute.id])

        # Negative amount
        self.poster_client.post(url, {'offered_doer_amount': -10})
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.offered_doer_amount)

        # Amount exceeding task reward
        self.poster_client.post(url, {'offered_doer_amount': 150})
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.offered_doer_amount)

        # Non-integer amount
        self.poster_client.post(url, {'offered_doer_amount': 'abc'})
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.offered_doer_amount)

    def test_propose_settlement_unauthorized_user(self):
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.third_party_client.post(url, {'offered_doer_amount': 50})
        self.assertRedirects(response, reverse('home'))

        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.offered_doer_amount)

    def test_accept_settlement_flow(self):
        # Poster proposes 60 points doer payout (40 points poster refund)
        propose_url = reverse('propose_settlement', args=[self.dispute.id])
        self.poster_client.post(propose_url, {'offered_doer_amount': 60})

        poster_rewards_before = self.poster_profile.rewards
        doer_rewards_before = self.doer_profile.rewards

        # Doer accepts proposal
        accept_url = reverse('accept_settlement', args=[self.dispute.id])
        response = self.doer_client.post(accept_url)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        # Refresh objects
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.doer_profile.refresh_from_db()

        # Check balances
        self.assertEqual(self.doer_profile.rewards, doer_rewards_before + 60)
        self.assertEqual(self.poster_profile.rewards, poster_rewards_before + 40)

        # Verify balance conservation (zero point creation/destruction invariant)
        points_distributed = 60 + 40
        self.assertEqual(points_distributed, self.task.reward)

        # Check RewardLedger entries
        doer_ledger = RewardLedger.objects.get(user=self.doer, transaction_type='dispute_payout')
        self.assertEqual(doer_ledger.amount, 60)
        self.assertEqual(doer_ledger.task, self.task)

        poster_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='dispute_refund')
        self.assertEqual(poster_ledger.amount, 40)
        self.assertEqual(poster_ledger.task, self.task)

        # Check statuses
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'partial_split')
        self.assertEqual(self.dispute.resolved_doer_amount, 60)
        self.assertEqual(self.dispute.resolved_poster_amount, 40)
        self.assertEqual(self.task.status, 'completed')

    def test_accept_settlement_proposer_cannot_accept_own_offer(self):
        propose_url = reverse('propose_settlement', args=[self.dispute.id])
        self.poster_client.post(propose_url, {'offered_doer_amount': 60})

        accept_url = reverse('accept_settlement', args=[self.dispute.id])
        response = self.poster_client.post(accept_url)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_reject_settlement_flow(self):
        propose_url = reverse('propose_settlement', args=[self.dispute.id])
        self.poster_client.post(propose_url, {'offered_doer_amount': 60})

        reject_url = reverse('reject_settlement', args=[self.dispute.id])
        response = self.doer_client.post(reject_url)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.offered_doer_amount)
        self.assertIsNone(self.dispute.offered_by)

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('rejected', notification.message)

    def test_dispute_detail_template_rendering(self):
        detail_url = reverse('dispute_detail', args=[self.dispute.id])

        # Render open dispute detail page
        response = self.poster_client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Escrow Balance Reserved:')
        self.assertContains(response, '100 points')

        # Propose settlement and check rendering
        propose_url = reverse('propose_settlement', args=[self.dispute.id])
        self.poster_client.post(propose_url, {'offered_doer_amount': 70})

        response = self.doer_client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Active Settlement Proposal')
        self.assertContains(response, '70 points')
        self.assertContains(response, 'Accept Proposal')
        self.assertContains(response, 'Reject Proposal')

        # Accept settlement and check resolved summary rendering
        accept_url = reverse('accept_settlement', args=[self.dispute.id])
        self.doer_client.post(accept_url)

        response = self.poster_client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Resolution Summary')
        self.assertContains(response, '70 points')
        self.assertContains(response, '30 points')
