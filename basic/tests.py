import html
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger, Notification


class TaskAbandonmentTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=1000)

        self.client = Client()

    def test_transaction_types_contains_task_abandonment(self):
        transaction_types_dict = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('task_abandonment', transaction_types_dict)
        self.assertEqual(transaction_types_dict['task_abandonment'], 'Task Abandonment Penalty')

    def test_abandon_task_deducts_proportional_penalty_and_logs_ledger(self):
        self.client.login(username='taker', password='password123')

        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # 1. UserProfile rewards updated (1500 - 20% of 100 = 1480)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1480)

        # 2. RewardLedger entry created
        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)
        self.assertEqual(ledger_entry.task, task)
        self.assertIn("Test Task", ledger_entry.description)

        # 3. Task status reset
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # 4. Poster notification sent
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker', notification.message)
        self.assertIn('Test Task', notification.message)

        # 5. Check rewards view renders transaction history
        rewards_response = self.client.get(reverse('rewards'))
        self.assertEqual(rewards_response.status_code, 200)
        self.assertContains(rewards_response, '-20')
        self.assertContains(rewards_response, html.escape(f"Penalty for abandoning task: '{task.title}'"))

    def test_abandon_task_minimum_penalty(self):
        self.client.login(username='taker', password='password123')

        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='Small Task',
            description='Small Description',
            reward=20, # 20% of 20 = 4, minimum penalty is 10
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1490) # 1500 - 10

        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -10)

    def test_abandon_task_negative_balance_allowed(self):
        self.taker_profile.rewards = 5
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')

        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='Big Task',
            description='Big Description',
            reward=100, # Penalty 20
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, -15) # 5 - 20 = -15

    def test_abandon_task_not_taken_by_user(self):
        self.client.login(username='other', password='password123')

        deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=deadline
        )

        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertEqual(response.status_code, 404)

        # Verify no points deducted from either user
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1500)
        self.other_profile.refresh_from_db()
        self.assertEqual(self.other_profile.rewards, 1000)
