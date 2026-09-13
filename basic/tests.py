from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import Task, UserProfile, RewardLedger, Notification


class TaskAbandonmentPenaltyTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.client = Client()

    def test_reward_ledger_choices_contains_task_abandonment(self):
        choices = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('task_abandonment', choices)
        self.assertEqual(choices['task_abandonment'], 'Task Abandonment (Penalty Deduction)')

    def test_abandon_task_deducts_proportional_penalty(self):
        # Create task with reward 500
        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='High Value Task',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', kwargs={'task_id': task.id}), follow=True)

        self.assertEqual(response.status_code, 200)

        # Reload taker profile and task
        self.taker_profile.refresh_from_db()
        task.refresh_from_db()

        # Calculated penalty = 20% of 500 = 100 points
        self.assertEqual(self.taker_profile.rewards, 400) # 500 - 100 = 400
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -100)
        self.assertEqual(ledger.description, "Proportional penalty (100 pts) for abandoning task: 'High Value Task'")

        # Check Notification to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn("has abandoned your task: 'High Value Task'", notification.message)

    def test_abandon_task_low_balance_protected_by_zero_floor(self):
        # Set taker rewards to 30 points (less than calculated 100 point penalty)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='High Value Task',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', kwargs={'task_id': task.id}), follow=True)

        self.assertEqual(response.status_code, 200)

        self.taker_profile.refresh_from_db()
        task.refresh_from_db()

        # Deducted penalty capped at 30, balance becomes 0 (not negative)
        self.assertEqual(self.taker_profile.rewards, 0)
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -30)
        self.assertEqual(ledger.description, "Proportional penalty (100 pts) for abandoning task: 'High Value Task'")

    def test_abandon_task_minimum_one_point_penalty(self):
        # Small task reward = 3 (20% of 3 is 0.6 -> int is 0 -> max(1, 0) = 1)
        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='Small Task',
            description='Test Description',
            reward=3,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', kwargs={'task_id': task.id}), follow=True)

        self.assertEqual(response.status_code, 200)

        self.taker_profile.refresh_from_db()

        # Penalty of 1 point deducted from 500
        self.assertEqual(self.taker_profile.rewards, 499)

        ledger = RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -1)
        self.assertEqual(ledger.description, "Proportional penalty (1 pts) for abandoning task: 'Small Task'")

