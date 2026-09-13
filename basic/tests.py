from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import math

from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


class PartialDisputeSettlementTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        
        # Create doer/taker
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)

        # Create bystander
        self.bystander = User.objects.create_user(username='bystander', password='password123')
        self.bystander_profile = UserProfile.objects.create(user=self.bystander, rewards=1000)

        # Create a task posted by poster, taken by doer
        self.task = Task.objects.create(
            title="Build Partial Dispute Feature",
            description="Implement escrow split logic",
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )

        # Create conversation for taken task
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.doer)

        # Poster's balance reduced by 100 on task creation
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-100,
            transaction_type='task_creation', description="Reserved for task"
        )

        # Raise dispute by doer
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason="Work partially completed, need to split reward."
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_propose_settlement_valid(self):
        self.client.login(username='doer', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url, {'proposed_taker_pct': 60}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposed_taker_pct, 60)
        self.assertEqual(self.dispute.proposed_by, self.doer)
        self.assertEqual(self.dispute.proposal_status, 'pending')

        # Notification to poster
        noti = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(noti)
        self.assertIn("60% to taker", noti.message)

    def test_propose_settlement_invalid_percentages(self):
        self.client.login(username='doer', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])

        invalid_percentages = [0, 100, -10, 150, "invalid"]
        for invalid_pct in invalid_percentages:
            response = self.client.post(url, {'proposed_taker_pct': invalid_pct}, follow=True)
            self.dispute.refresh_from_db()
            self.assertEqual(self.dispute.proposal_status, 'none')
            self.assertIsNone(self.dispute.proposed_taker_pct)

    def test_propose_settlement_unauthorized(self):
        self.client.login(username='bystander', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url, {'proposed_taker_pct': 50})

        self.assertRedirects(response, reverse('home'))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposal_status, 'none')

    def test_decline_settlement_and_counter_offer(self):
        # Doer proposes 80%
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 80})

        # Poster logs in and declines
        self.client.login(username='poster', password='password123')
        url_respond = reverse('respond_settlement', args=[self.dispute.id])
        response = self.client.post(url_respond, {'action': 'decline'}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposal_status, 'declined')

        # Notification to doer
        noti = Notification.objects.filter(recipient=self.doer).first()
        self.assertIsNotNone(noti)
        self.assertIn("declined", noti.message)

        # Poster makes counter-offer of 50%
        url_propose = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url_propose, {'proposed_taker_pct': 50}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposed_taker_pct, 50)
        self.assertEqual(self.dispute.proposed_by, self.poster)
        self.assertEqual(self.dispute.proposal_status, 'pending')

    def test_accept_settlement_atomic_execution(self):
        # Doer proposes 60%
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 60})

        initial_doer_rewards = self.doer_profile.rewards
        initial_poster_rewards = self.poster_profile.rewards

        # Poster accepts 60% offer
        self.client.login(username='poster', password='password123')
        url_respond = reverse('respond_settlement', args=[self.dispute.id])
        response = self.client.post(url_respond, {'action': 'accept'}, follow=True)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.proposal_status, 'accepted')
        self.assertEqual(self.task.status, 'completed')

        # Balance updates: 60 points to doer, 40 points to poster
        self.assertEqual(self.doer_profile.rewards, initial_doer_rewards + 60)
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 40)

        # RewardLedger entries check
        payout_ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_partial_payout').first()
        refund_ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_partial_refund').first()

        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.user, self.doer)
        self.assertEqual(payout_ledger.amount, 60)

        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.user, self.poster)
        self.assertEqual(refund_ledger.amount, 40)

        # Total points disbursed equals total task reward
        self.assertEqual(payout_ledger.amount + refund_ledger.amount, self.task.reward)

    def test_math_precision_and_conservation_odd_splits(self):
        # Task with 33 points reward
        odd_task = Task.objects.create(
            title="Odd reward task",
            description="33 reward points",
            reward=33,
            posted_by=self.poster,
            taken_by=self.doer,
            status='in_progress'
        )
        odd_conversation = Conversation.objects.create(task=odd_task)
        odd_conversation.participants.add(self.poster, self.doer)

        odd_dispute = Dispute.objects.create(
            task=odd_task, raised_by=self.doer, reason="Odd split"
        )
        odd_task.status = 'disputed'
        odd_task.save()

        # Propose 40% split (floor(33 * 0.40) = 13, poster gets 33 - 13 = 20)
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[odd_dispute.id]), {'proposed_taker_pct': 40})

        initial_doer_rewards = self.doer_profile.rewards
        initial_poster_rewards = self.poster_profile.rewards

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('respond_settlement', args=[odd_dispute.id]), {'action': 'accept'})

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        doer_gain = self.doer_profile.rewards - initial_doer_rewards
        poster_gain = self.poster_profile.rewards - initial_poster_rewards

        self.assertEqual(doer_gain, 13)
        self.assertEqual(poster_gain, 20)
        self.assertEqual(doer_gain + poster_gain, 33)

    def test_state_locking_complete_task_while_proposal_pending(self):
        # Doer proposes settlement
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 50})

        # Poster attempts to call complete_task while proposal is pending
        self.client.login(username='poster', password='password123')
        url_complete = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url_complete, follow=True)

        self.task.refresh_from_db()
        self.assertNotEqual(self.task.status, 'completed')
        self.assertEqual(self.task.status, 'disputed')
        messages_list = [m.message for m in response.context['messages']]
        self.assertTrue(any("Cannot complete task while a settlement proposal is pending" in m for m in messages_list))
