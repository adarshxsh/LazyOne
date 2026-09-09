from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, RewardLedger, Conversation
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

class CollateralLockupAndSlashingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.poor_taker = User.objects.create_user(username='poor_taker', password='password123')
        self.poor_taker_profile = UserProfile.objects.create(user=self.poor_taker, rewards=20)

        self.client = Client()

    def test_collateral_amount_calculation(self):
        task1 = Task.objects.create(
            title='Test Task 1', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        task2 = Task.objects.create(
            title='Test Task 2', description='Desc', reward=15,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.assertEqual(task1.collateral_amount, 50)
        self.assertEqual(task2.collateral_amount, 7)

    def test_take_task_insufficient_collateral_fails(self):
        task = Task.objects.create(
            title='High Reward Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='poor_taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.poor_taker_profile.refresh_from_db()

        # Task should remain available and untaken
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        # Balance should remain unchanged
        self.assertEqual(self.poor_taker_profile.rewards, 20)
        # No collateral lock entry in RewardLedger
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='collateral_lock').exists())

    def test_take_task_sufficient_collateral_locks_points(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Task assigned and status in_progress
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)
        # 50 points locked from 100 available -> 50 remaining
        self.assertEqual(self.taker_profile.rewards, 50)

        # RewardLedger collateral lock entry exists
        ledger_entry = RewardLedger.objects.get(
            user=self.taker, task=task, transaction_type='collateral_lock'
        )
        self.assertEqual(ledger_entry.amount, -50)

    def test_complete_task_releases_collateral_and_awards_reward(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        # Taker starts with 100, -50 lock + 100 reward + 50 collateral release = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_completion', amount=100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=50).exists())

    def test_accept_cancellation_refunds_collateral(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        # Taker collateral refunded: 50 remaining + 50 refunded = 100
        self.assertEqual(self.taker_profile.rewards, 100)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_refund', amount=50).exists())

    def test_abandon_task_slashes_collateral_to_poster(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task (taker balance 100 -> 50)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Taker abandons task
        self.client.get(reverse('abandon_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Task is back to available and unassigned
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Taker balance remains 50 (the locked 50 was slashed)
        self.assertEqual(self.taker_profile.rewards, 50)

        # Poster receives the slashed 50 collateral points (1000 initial + 50 slashed = 1050)
        self.assertEqual(self.poster_profile.rewards, 1050)

        # Slashing penalty transaction entries exist in RewardLedger
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, task=task, transaction_type='collateral_slash', amount=50).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_slash', amount=-50).exists())
