from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger, Conversation

class TaskCollateralTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)
        self.taker_profile.rewards = 100
        self.taker_profile.save()

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            deadline=self.deadline,
            status='available'
        )

    def test_collateral_required_calculation(self):
        # 20% of 100 = 20
        self.assertEqual(self.task.collateral_required, 20)

        # 20% of 10 = 2
        task10 = Task(reward=10)
        self.assertEqual(task10.collateral_required, 2)

        # 20% of 3 = 0.6 -> rounded up to 1
        task3 = Task(reward=3)
        self.assertEqual(task3.collateral_required, 1)

        # 20% of 1 = 0.2 -> minimum 1
        task1 = Task(reward=1)
        self.assertEqual(task1.collateral_required, 1)

    def test_take_task_insufficient_balance(self):
        # Set taker rewards lower than 20% of task reward (20)
        self.taker_profile.rewards = 10
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 10)

        self.assertFalse(RewardLedger.objects.filter(transaction_type='collateral_lock').exists())

    def test_take_task_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)

        self.taker_profile.refresh_from_db()
        # 100 - 20 (collateral) = 80
        self.assertEqual(self.taker_profile.rewards, 80)

        ledger_entry = RewardLedger.objects.get(
            user=self.taker,
            task=self.task,
            transaction_type='collateral_lock'
        )
        self.assertEqual(ledger_entry.amount, -20)

    def test_complete_task_success(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        # Initial 100 - 20 (lock) + 100 (reward) + 20 (release) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        self.assertTrue(RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='task_completion',
            amount=100
        ).exists())

        self.assertTrue(RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='collateral_release',
            amount=20
        ).exists())

    def test_abandon_task_slashes_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        initial_poster_rewards = self.poster_profile.rewards

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker_profile.refresh_from_db()
        # Taker balance remains deducted by collateral (80)
        self.assertEqual(self.taker_profile.rewards, 80)

        self.poster_profile.refresh_from_db()
        # Poster balance credited with slashed collateral (+20)
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 20)

        self.assertTrue(RewardLedger.objects.filter(
            user=self.poster,
            task=self.task,
            transaction_type='collateral_slash',
            amount=20
        ).exists())

    def test_accept_cancellation_releases_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker_profile.refresh_from_db()
        # Collateral refunded: 80 + 20 = 100
        self.assertEqual(self.taker_profile.rewards, 100)

        self.assertTrue(RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='collateral_release',
            amount=20
        ).exists())
