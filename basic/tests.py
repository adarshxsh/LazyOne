from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import Task, UserProfile, RewardLedger


class TaskCancellationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=1500)

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        # Poster creates a task
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-100,
            transaction_type='task_creation', description="Reserved for task: 'Test Task'"
        )

    def test_accept_cancellation_terminal_state(self):
        # 1. Taker claims task
        response = self.client_taker.get(reverse('take_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)

        # 2. Poster requests cancellation
        response = self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # 3. Taker accepts cancellation
        response = self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Requirement 1: Task status must be 'cancelled'
        self.assertEqual(self.task.status, 'cancelled')
        self.assertFalse(self.task.cancellation_requested)

        # Requirement 2: Reward points returned to poster balance exactly once
        self.assertEqual(self.poster_profile.rewards, 1500)

        # Requirement 3: Ledger record logged for the refund
        refund_ledger = RewardLedger.objects.filter(
            user=self.poster,
            task=self.task,
            transaction_type='task_cancellation'
        )
        self.assertEqual(refund_ledger.count(), 1)
        self.assertEqual(refund_ledger.first().amount, 100)

        # Requirement 4: Cancelled task is not in available task list
        available_tasks = Task.objects.filter(status='available')
        self.assertNotIn(self.task, available_tasks)

        # Acceptance Criteria: Other users cannot take or complete the cancelled task
        take_response = self.client_other.get(reverse('take_task', args=[self.task.id]))
        self.assertEqual(take_response.status_code, 404)

        complete_response = self.client_poster.get(reverse('complete_task', args=[self.task.id]))
        self.assertEqual(complete_response.status_code, 404)

        # Cannot re-accept cancellation
        reaccept_response = self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertEqual(reaccept_response.status_code, 404)

