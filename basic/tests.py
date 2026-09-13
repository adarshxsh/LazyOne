from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, RewardLedger, Conversation, calculate_collateral
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta


class CollateralMechanicsTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create Poster user and profile
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        
        # Create Taker user and profile with 100 points
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 100})

        # Create Sybil user and profile with 0 points
        self.sybil = User.objects.create_user(username='sybil', password='password123')
        self.sybil_profile, _ = UserProfile.objects.get_or_create(user=self.sybil, defaults={'rewards': 0})

    def test_calculate_collateral(self):
        # 20% of reward >= 10
        self.assertEqual(calculate_collateral(100), 20)
        self.assertEqual(calculate_collateral(50), 10)
        
        # 20% of reward < 10, floor of 10 applied
        self.assertEqual(calculate_collateral(40), 10)
        self.assertEqual(calculate_collateral(15), 10)
        
        # Reward < 10, collateral capped at reward
        self.assertEqual(calculate_collateral(8), 8)
        self.assertEqual(calculate_collateral(5), 5)

    def test_sybil_insufficient_collateral_rejection(self):
        # Create a task with 100 reward (requires 20 collateral)
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Sybil has 0 points, cannot afford 20 collateral
        self.client.login(username='sybil', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.sybil_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(self.sybil_profile.rewards, 0)
        self.assertFalse(RewardLedger.objects.filter(user=self.sybil, transaction_type='collateral_lock').exists())

    def test_take_task_collateral_lock(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Collateral is 20 points
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)
        self.assertEqual(task.collateral_amount, 20)
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20

        lock_ledger = RewardLedger.objects.get(user=self.taker, task=task, transaction_type='collateral_lock')
        self.assertEqual(lock_ledger.amount, -20)

    def test_complete_task_restores_collateral_and_awards_reward(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.collateral_amount, 0)
        # Initial 100 - 20 (lock) + 20 (release) + 100 (completion) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=20).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_completion', amount=100).exists())

    def test_accept_cancellation_restores_collateral(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[task.id]), follow=True)

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(task.collateral_amount, 0)
        # Taker balance restored to 100 (100 - 20 + 20)
        self.assertEqual(self.taker_profile.rewards, 100)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=20).exists())

    def test_abandon_task_slashes_collateral_and_indemnifies_poster(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task (locking 20 collateral)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        initial_poster_rewards = self.poster_profile.rewards

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(task.collateral_amount, 0)
        # Taker's points remain at 80 (20 collateral slashed/lost)
        self.assertEqual(self.taker_profile.rewards, 80)
        # Poster receives 20 points indemnity
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 20)

        slash_ledger = RewardLedger.objects.get(user=self.poster, task=task, transaction_type='collateral_slashing')
        self.assertEqual(slash_ledger.amount, 20)
