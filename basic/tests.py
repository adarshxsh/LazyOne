from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.contrib.messages import get_messages
from basic.models import UserProfile, Task, RewardLedger, Notification
from django.utils import timezone
from datetime import timedelta


class TaskAbandonmentPenaltyTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        self.client = Client()

    def test_transaction_types_contains_task_abandonment(self):
        types_dict = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('task_abandonment', types_dict)
        self.assertEqual(types_dict['task_abandonment'], 'Task Abandonment Penalty')

    def test_abandon_task_even_reward(self):
        task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('abandon_task', kwargs={'task_id': task.id}))

        self.assertRedirects(response, reverse('my_tasks'))

        # Check taker reward points deducted by 50%
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1450) # 1500 - 50

        # Check task reset
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Check RewardLedger
        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -50)
        self.assertEqual(ledger_entry.task, task)

        # Check Notification to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker has abandoned your task: \'Test Task\'', notification.message)

        # Check warning message
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("penalty of 50 points" in str(m) for m in messages))
        self.assertTrue(any(m.level_tag == "warning" for m in messages))

    def test_abandon_task_odd_reward(self):
        task = Task.objects.create(
            title='Odd Reward Task',
            description='Test Description',
            reward=25,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('abandon_task', kwargs={'task_id': task.id}))

        self.assertRedirects(response, reverse('my_tasks'))

        # Penalty = 25 // 2 = 12
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1488) # 1500 - 12

        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -12)

    def test_rewards_dashboard_renders_penalty_transaction(self):
        task = Task.objects.create(
            title='Dashboard Task',
            description='Test Description',
            reward=60,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='taker', password='password123')
        self.client.post(reverse('abandon_task', kwargs={'task_id': task.id}))

        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "-30")
        self.assertContains(response, "Penalty for abandoning task: &#x27;Dashboard Task&#x27;" if "&#x27;" in response.content.decode() else "Penalty for abandoning task: 'Dashboard Task'")

    def test_abandon_task_unauthorized(self):
        other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=other_user, rewards=1500)

        task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('abandon_task', kwargs={'task_id': task.id}))
        self.assertEqual(response.status_code, 404)
