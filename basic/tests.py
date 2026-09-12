from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger


class TaskCollateralSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create Poster User
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.poster_profile.rewards = 1500
        self.poster_profile.save()

        # Create Taker User with sufficient rewards
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        # Create Taker User with low rewards
        self.poor_taker = User.objects.create_user(username='poor_taker', password='password123')
        self.poor_taker_profile, _ = UserProfile.objects.get_or_create(user=self.poor_taker)
        self.poor_taker_profile.rewards = 10
        self.poor_taker_profile.save()

        # Create Task
        deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title="Test Collateral Task",
            description="Test Description",
            reward=100,  # 20% collateral = 20 points
            posted_by=self.poster,
            deadline=deadline,
            status='available'
        )

    def test_reward_ledger_choices_validation(self):
        """Verify RewardLedger choices contain all new collateral transaction types and pass validation."""
        choices_dict = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('collateral_lock', choices_dict)
        self.assertIn('collateral_refund', choices_dict)
        self.assertIn('collateral_slash', choices_dict)

        lock_entry = RewardLedger(
            user=self.taker, task=self.task, amount=-20,
            transaction_type='collateral_lock', description="Lock test"
        )
        lock_entry.full_clean()

        refund_entry = RewardLedger(
            user=self.taker, task=self.task, amount=20,
            transaction_type='collateral_refund', description="Refund test"
        )
        refund_entry.full_clean()

        slash_entry = RewardLedger(
            user=self.poster, task=self.task, amount=20,
            transaction_type='collateral_slash', description="Slash test"
        )
        slash_entry.full_clean()

    def test_take_task_rejects_insufficient_collateral(self):
        """take_task rejects claims if user reward balance is less than 20% collateral requirement."""
        self.client.login(username='poor_taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.poor_taker_profile.refresh_from_db()

        # Task should remain available and untaken
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.collateral_amount, 0)
        # Balance should be unchanged
        self.assertEqual(self.poor_taker_profile.rewards, 10)
        # No collateral lock ledger entry created
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='collateral_lock').exists())

    def test_take_task_locks_collateral(self):
        """take_task locks collateral points, updates Task.collateral_amount, and logs collateral_lock."""
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        self.assertEqual(self.task.collateral_amount, 20)
        # 500 initial - 20 collateral locked = 480
        self.assertEqual(self.taker_profile.rewards, 480)

        ledger_entry = RewardLedger.objects.get(
            user=self.taker, task=self.task, transaction_type='collateral_lock'
        )
        self.assertEqual(ledger_entry.amount, -20)

    def test_poster_cannot_take_own_task(self):
        """Poster cannot take their own task."""
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

    def test_complete_task_refunds_collateral(self):
        """complete_task restores locked collateral to taker alongside task reward and logs collateral_refund."""
        # Claim task first
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        # 480 balance after claim + 100 reward + 20 collateral refund = 600 total (net +100 from initial 500)
        self.assertEqual(self.taker_profile.rewards, 600)

        completion_ledger = RewardLedger.objects.get(
            user=self.taker, task=self.task, transaction_type='task_completion'
        )
        self.assertEqual(completion_ledger.amount, 100)

        refund_ledger = RewardLedger.objects.get(
            user=self.taker, task=self.task, transaction_type='collateral_refund'
        )
        self.assertEqual(refund_ledger.amount, 20)

    def test_abandon_task_slashes_collateral(self):
        """abandon_task slashes locked collateral, transfers points to poster, and logs collateral_slash."""
        # Claim task first
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Task should be reset to available with collateral_amount reset to 0
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.collateral_amount, 0)

        # Taker balance remains at 480 (locked collateral lost)
        self.assertEqual(self.taker_profile.rewards, 480)
        # Poster gets 20 slashed collateral points: 1500 + 20 = 1520
        self.assertEqual(self.poster_profile.rewards, 1520)

        slash_ledger = RewardLedger.objects.get(
            user=self.poster, task=self.task, transaction_type='collateral_slash'
        )
        self.assertEqual(slash_ledger.amount, 20)

    def test_accept_cancellation_refunds_collateral(self):
        """accept_cancellation refunds locked collateral to taker and task reward to poster."""
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.collateral_amount, 0)

        # Taker gets collateral refunded (480 + 20 = 500)
        self.assertEqual(self.taker_profile.rewards, 500)
        # Poster gets reward refunded (1500 + 100 = 1600)
        self.assertEqual(self.poster_profile.rewards, 1600)

        refund_ledger = RewardLedger.objects.get(
            user=self.taker, task=self.task, transaction_type='collateral_refund'
        )
        self.assertEqual(refund_ledger.amount, 20)
