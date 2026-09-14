from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, RewardLedger, Notification


class TaskAbandonmentPenaltyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )

    def test_reward_ledger_transaction_types_contains_task_abandonment(self):
        types_dict = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('task_abandonment', types_dict)
        self.assertEqual(types_dict['task_abandonment'], 'Task Abandonment (Penalty Deducted)')

    def test_abandon_task_deducts_penalty_and_creates_ledger(self):
        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', args=[self.task.id])
        response = self.client.get(url, follow=True)

        self.assertRedirects(response, reverse('my_tasks'))

        # Check taker balance (20% of 100 = 20 points penalty)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 480)

        # Check Task status
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Check RewardLedger
        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)
        self.assertEqual(ledger_entry.task, self.task)

        # Check Notification to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('abandoned', notification.message)

    def test_abandon_task_minimum_penalty(self):
        self.task.reward = 3
        self.task.save()

        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', args=[self.task.id])
        self.client.get(url)

        self.taker_profile.refresh_from_db()
        # 20% of 3 = 0.6 -> min penalty is 1 point.
        self.assertEqual(self.taker_profile.rewards, 499)

        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertEqual(ledger_entry.amount, -1)

    def test_abandon_task_floors_rewards_at_zero(self):
        self.taker_profile.rewards = 5
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', args=[self.task.id])
        self.client.get(url)

        self.taker_profile.refresh_from_db()
        # Balance was 5, penalty is 20 -> floors at 0
        self.assertEqual(self.taker_profile.rewards, 0)

        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertEqual(ledger_entry.amount, -20)

    def test_abandon_task_requires_taken_by_current_user(self):
        other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=other_user, rewards=500)

        self.client.login(username='other', password='password123')
        url = reverse('abandon_task', args=[self.task.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

