from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, RewardLedger, UserProfile, Conversation
from django.conf import settings


class DisputeAndAbandonmentTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_both_poster_and_taker_can_raise_dispute(self):
        # Taker raises dispute
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Taker dispute reason'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        self.assertEqual(self.task.dispute.reason, 'Taker dispute reason')

        # Clean up dispute for poster test
        self.task.dispute.delete()
        self.task.status = 'in_progress'
        self.task.save()

        # Poster raises dispute
        response = self.client_poster.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster dispute reason'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Poster dispute reason')

    def test_unauthorized_user_cannot_raise_dispute(self):
        response = self.client_other.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unauthorized dispute'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_task_abandonment_blocked_during_active_dispute(self):
        # Raise dispute first
        Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        initial_taker_rewards = self.taker.userprofile.rewards

        # Taker attempts to abandon task
        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.task.taken_by, self.taker)

        # Check explicit error message in response context/messages
        messages = list(response.context['messages']) if response.context and 'messages' in response.context else []
        message_texts = [str(m) for m in messages]
        self.assertTrue(any('blocked' in m.lower() or 'dispute' in m.lower() for m in message_texts))

        # Ensure no points were deducted and no abandonment ledger record created
        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards)
        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').exists())

    def test_abandon_undisputed_task_deducts_penalty_and_creates_ledger(self):
        initial_rewards = self.taker.userprofile.rewards
        penalty = getattr(settings, 'ABANDONMENT_PENALTY', 50)

        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_rewards - penalty)

        ledger_entry = RewardLedger.objects.get(
            user=self.taker,
            task=self.task,
            transaction_type='task_abandonment'
        )
        self.assertEqual(ledger_entry.amount, -penalty)

    def test_abandonment_penalty_deduction_allows_negative_balance(self):
        profile = self.taker.userprofile
        profile.rewards = 20
        profile.save()

        penalty = getattr(settings, 'ABANDONMENT_PENALTY', 50)

        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        profile.refresh_from_db()
        self.assertEqual(profile.rewards, 20 - penalty)
        self.assertLess(profile.rewards, 0)

    def test_poster_initiated_mutual_cancellation_has_no_taker_penalty(self):
        initial_taker_rewards = self.taker.userprofile.rewards

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker accepts cancellation
        self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance should remain unchanged
        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards)

        # No abandonment penalty ledger entry for taker
        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').exists())
