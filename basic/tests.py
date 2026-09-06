from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, RewardLedger

class CollateralAndSlashingTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create Poster user
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster)[0]
        self.poster_profile.rewards = 500
        self.poster_profile.save()

        # Create Taker user
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.get_or_create(user=self.taker)[0]
        self.taker_profile.rewards = 100
        self.taker_profile.save()

        # Create Poor Taker user
        self.poor_taker = User.objects.create_user(username='poortaker', password='password123')
        self.poor_taker_profile = UserProfile.objects.get_or_create(user=self.poor_taker)[0]
        self.poor_taker_profile.rewards = 5  # Less than required collateral for 100 reward task (20 pts)
        self.poor_taker_profile.save()

        # Create Task
        deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            deadline=deadline,
            status='available'
        )

    def test_collateral_calculation(self):
        # 20% default collateral ratio
        self.assertEqual(self.task.required_collateral, 20)
        self.assertEqual(self.task.collateral_required, 20)
        self.assertEqual(self.task.collateral, 20)

        # Task with 50 reward -> 10 collateral
        task50 = Task.objects.create(
            title='Task 50', description='Desc', reward=50, posted_by=self.poster
        )
        self.assertEqual(task50.required_collateral, 10)

        # Task with 5 reward -> 1 collateral (ceil(5 * 0.2) = 1)
        task5 = Task.objects.create(
            title='Task 5', description='Desc', reward=5, posted_by=self.poster
        )
        self.assertEqual(task5.required_collateral, 1)

    def test_take_task_insufficient_collateral(self):
        self.client.login(username='poortaker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        self.poor_taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        # Check that user could not claim task
        self.assertEqual(self.poor_taker_profile.rewards, 5)
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.locked_collateral, 0)

        # Check message
        messages = list(response.context['messages'])
        self.assertTrue(any('Insufficient balance' in str(m) for m in messages))

    def test_take_task_sufficient_collateral(self):
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        self.taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        # Check collateral locked and balance deducted
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        self.assertEqual(self.task.locked_collateral, 20)

        # Check RewardLedger
        ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='collateral_lock')
        self.assertEqual(ledger.amount, -20)

    def test_complete_task_releases_collateral_and_reward(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]), follow=True)

        self.taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        # Taker gets reward (100) + locked collateral (20) back -> 80 + 100 + 20 = 200
        self.assertEqual(self.taker_profile.rewards, 200)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.locked_collateral, 0)

        # Check ledger entries for taker
        completion_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='task_completion')
        self.assertEqual(completion_ledger.amount, 100)

        unlock_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='collateral_unlock')
        self.assertEqual(unlock_ledger.amount, 20)

    def test_abandon_task_forfeits_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.task.refresh_from_db()

        # Task is reset to available
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.locked_collateral, 0)

        # Taker's points remain deducted (collateral forfeited) -> 80 pts
        self.assertEqual(self.taker_profile.rewards, 80)

        # Poster's points unchanged
        self.assertEqual(self.poster_profile.rewards, 500)

        # Check penalty ledger record
        forfeit_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='collateral_forfeit')
        self.assertEqual(forfeit_ledger.amount, -20)

    def test_accept_cancellation_unlocks_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]), follow=True)

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[self.task.id]), follow=True)

        self.taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        # Collateral is unlocked back to taker -> 80 + 20 = 100
        self.assertEqual(self.taker_profile.rewards, 100)
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

    def test_poster_cannot_claim_own_task(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
