from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, UserProfile, RewardLedger, Conversation, Dispute


class CollateralAndSlashingEngineTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster user and profile
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.poster_profile.rewards = 1500
        self.poster_profile.save()

        # Create taker user and profile
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.taker_profile.rewards = 1500
        self.taker_profile.save()

        # Create an entry-level task (reward = 100 points)
        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Sample Task',
            description='Task description',
            reward=100,
            posted_by=self.poster,
            deadline=self.deadline,
            status='available'
        )
        # Deduct poster points for task creation to simulate add_task view logic
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: 'Sample Task'"
        )

    def test_take_task_insufficient_points_fails(self):
        # Set taker balance below required collateral (100)
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.taker_profile.rewards, 50)
        self.assertFalse(
            RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_lock').exists()
        )

    def test_take_task_sufficient_points_locks_collateral(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        # Taker balance should be reduced by 100 (1500 - 100 = 1400)
        self.assertEqual(self.taker_profile.rewards, 1400)

        ledger_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_lock')
        self.assertEqual(ledger_entry.amount, -100)
        self.assertEqual(ledger_entry.task, self.task)

    def test_task_completion_releases_collateral_and_pays_reward(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        # Taker profile gets 100 (collateral release) + 100 (task reward) = 1600 total (1400 + 200)
        self.assertEqual(self.taker_profile.rewards, 1600)

        collateral_release_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_release')
        self.assertEqual(collateral_release_entry.amount, 100)

        task_completion_entry = RewardLedger.objects.get(user=self.taker, transaction_type='task_completion')
        self.assertEqual(task_completion_entry.amount, 100)

    def test_task_abandonment_slashes_collateral_and_compensates_poster(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Taker abandons task
        response = self.client.post(reverse('abandon_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance remains 1400 (lost 100 pts collateral)
        self.assertEqual(self.taker_profile.rewards, 1400)

        # Poster balance restored by 100 pts compensation (1400 + 100 = 1500)
        self.assertEqual(self.poster_profile.rewards, 1500)

        taker_slash_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_slash')
        self.assertEqual(taker_slash_entry.amount, -100)

        poster_slash_entry = RewardLedger.objects.get(user=self.poster, transaction_type='collateral_slash')
        self.assertEqual(poster_slash_entry.amount, 100)

    def test_accept_cancellation_releases_taker_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Poster points refunded: 1400 + 100 = 1500
        self.assertEqual(self.poster_profile.rewards, 1500)

        # Taker collateral released: 1400 + 100 = 1500
        self.assertEqual(self.taker_profile.rewards, 1500)

        self.assertTrue(
            RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_release').exists()
        )
