from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger

class TaskCollateralSlashingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.poor_taker = User.objects.create_user(username='poor_taker', password='password123')
        self.poor_taker_profile = UserProfile.objects.create(user=self.poor_taker, rewards=50)

        self.zero_taker = User.objects.create_user(username='zero_taker', password='password123')
        self.zero_taker_profile = UserProfile.objects.create(user=self.zero_taker, rewards=0)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=500,
            posted_by=self.poster,
            deadline=self.deadline,
            status='available'
        )

    def test_claim_task_insufficient_rewards_fails(self):
        self.client.login(username='poor_taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.poor_taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)
        self.assertEqual(self.poor_taker_profile.rewards, 50)
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='collateral_lock').exists())

    def test_claim_task_zero_balance_fails(self):
        self.client.login(username='zero_taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.zero_taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.zero_taker_profile.rewards, 0)

    def test_claim_task_sufficient_rewards_locks_collateral(self):
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        expected_collateral = int(500 * 0.2) # 100
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        self.assertEqual(self.task.taker_collateral, expected_collateral)
        self.assertEqual(self.taker_profile.rewards, 900)

        ledger = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_lock')
        self.assertEqual(ledger.amount, -expected_collateral)
        self.assertEqual(ledger.task, self.task)

    def test_complete_task_releases_collateral_and_payout(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.taker_collateral, 0)
        # Initial 1000 - 100 (collateral lock) + 500 (reward) + 100 (collateral release) = 1500
        self.assertEqual(self.taker_profile.rewards, 1500)

        completion_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='task_completion')
        self.assertEqual(completion_ledger.amount, 500)

        release_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_release')
        self.assertEqual(release_ledger.amount, 100)

    def test_abandon_task_slashes_collateral_to_poster(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)

        # Taker profile remains at 900 (100 locked and slashed)
        self.assertEqual(self.taker_profile.rewards, 900)

        # Poster profile credited 100 slashed collateral (1000 + 100 = 1100)
        self.assertEqual(self.poster_profile.rewards, 1100)

        slashed_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='collateral_slashed')
        self.assertEqual(slashed_ledger.amount, 100)
        self.assertEqual(slashed_ledger.task, self.task)

    def test_accept_cancellation_releases_collateral_without_slashing(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertEqual(self.task.taker_collateral, 0)

        # Poster refunded 500 reward (1000 + 500 = 1500)
        self.assertEqual(self.poster_profile.rewards, 1500)

        # Taker refunded 100 collateral (900 + 100 = 1000)
        self.assertEqual(self.taker_profile.rewards, 1000)

        release_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_release')
        self.assertEqual(release_ledger.amount, 100)

    def test_rewards_view_renders_correctly_with_collateral_transactions(self):
        # Perform claim and abandon
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.client.get(reverse('abandon_task', args=[self.task.id]))

        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Collateral locked")

        # Check poster rewards view
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Collateral slashed")
