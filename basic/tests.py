from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger, Conversation


class TaskAbandonmentRewardSlashingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client = Client()

    def test_abandon_task_deducts_penalty_and_creates_ledger(self):
        self.client.login(username='poster', password='password123')
        # Poster creates a task with reward = 100
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Build Feature',
            'description': 'Description here',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Build Feature')
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1400) # 1500 - 100

        # Taker takes the task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)

        # Taker abandons the task
        abandon_url = reverse('abandon_task', args=[task.id])
        response = self.client.get(abandon_url)
        self.assertRedirects(response, reverse('my_tasks'))

        # Check task state
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Check taker reward balance (1500 - 20 = 1480)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1480)

        # Check poster reward balance was not altered by abandonment
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1400)

        # Check ledger transaction entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='task_abandonment'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)
        self.assertEqual(ledger_entry.task, task)
        self.assertIn("Penalty for abandoned task", ledger_entry.description)
        self.assertIn("Build Feature", ledger_entry.description)

    def test_abandon_task_rounding(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        # Task with 15 points -> 20% of 15 is 3
        self.client.post(reverse('add_task'), {
            'title': 'Task 15',
            'description': 'Description',
            'reward': '15',
            'deadline': deadline
        })
        task15 = Task.objects.get(title='Task 15')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task15.id]))
        self.client.get(reverse('abandon_task', args=[task15.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1497) # 1500 - 3

        ledger15 = RewardLedger.objects.get(user=self.taker, task=task15)
        self.assertEqual(ledger15.amount, -3)

        # Task with 7 points -> 20% of 7 is 1.4 -> rounds to 1
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('add_task'), {
            'title': 'Task 7',
            'description': 'Description',
            'reward': '7',
            'deadline': deadline
        })
        task7 = Task.objects.get(title='Task 7')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task7.id]))
        self.client.get(reverse('abandon_task', args=[task7.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1496) # 1497 - 1

        ledger7 = RewardLedger.objects.get(user=self.taker, task=task7)
        self.assertEqual(ledger7.amount, -1)

    def test_abandon_task_appears_in_rewards_history(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Design Task',
            'description': 'Description',
            'reward': '50',
            'deadline': deadline
        })
        task = Task.objects.get(title='Design Task')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        self.client.get(reverse('abandon_task', args=[task.id]))

        # View rewards page
        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Penalty for abandoned task: &#x27;Design Task&#x27;")
        self.assertContains(response, "-10")

    def test_unauthorized_or_invalid_abandonment(self):
        task = Task.objects.create(
            title='Test Task',
            description='Desc',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        Conversation.objects.create(task=task)

        # Unauthenticated user redirected to login
        abandon_url = reverse('abandon_task', args=[task.id])
        response = self.client.get(abandon_url)
        self.assertRedirects(response, f"/login/?next={abandon_url}")

        # Non-taker user gets 404
        self.client.login(username='other_user', password='password123')
        response = self.client.get(abandon_url)
        self.assertEqual(response.status_code, 404)
