from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, RewardLedger, Notification, Conversation
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class TakerCollateralEscrowTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.poor_taker = User.objects.create_user(username='poortaker', password='password123')
        self.poor_taker_profile = UserProfile.objects.create(user=self.poor_taker, rewards=5)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

    def test_collateral_required_calculation(self):
        # Default TASK_COLLATERAL_PERCENTAGE is 20
        self.assertEqual(self.task.collateral_required, 20)

        # Test integer floor arithmetic with odd reward amounts
        odd_task = Task.objects.create(
            title='Odd Task',
            description='Odd Description',
            reward=15,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        # 15 * 20 / 100 = 3
        self.assertEqual(odd_task.collateral_required, 3)

        odd_task2 = Task.objects.create(
            title='Odd Task 2',
            description='Odd Description 2',
            reward=17,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        # 17 * 20 / 100 = 3.4 -> 3
        self.assertEqual(odd_task2.collateral_required, 3)

    def test_take_task_insufficient_collateral_rejected(self):
        self.client.login(username='poortaker', password='password123')
        # poor_taker has 5 rewards, but required collateral for 100 reward task is 20
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)

        self.poor_taker_profile.refresh_from_db()
        self.assertEqual(self.poor_taker_profile.rewards, 5)

        # Ensure no ledger deposit created
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='taker_collateral_deposit').exists())

    def test_take_task_success_locks_collateral(self):
        self.client.login(username='taker', password='password123')
        # taker has 100 rewards, required collateral is 20
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        self.assertEqual(self.task.taker_collateral, 20)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20

        # Verify ledger entry
        ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_deposit')
        self.assertEqual(ledger.amount, -20)

    def test_complete_task_refunds_collateral(self):
        # First, taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Now poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # Initial 100 - 20 (claim deposit) + 100 (task reward) + 20 (collateral refund) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        # Verify ledger entries
        refund_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_refund')
        self.assertEqual(refund_ledger.amount, 20)

        completion_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='task_completion')
        self.assertEqual(completion_ledger.amount, 100)

    def test_abandon_task_slashes_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80)

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)

        # Taker profile balance remains 80 (since 20 was deducted on claim and forfeited/slashed now)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80)
        self.assertGreaterEqual(self.taker_profile.rewards, 0)

        # Verify ledger slash entry
        slash_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_slash')
        self.assertEqual(slash_ledger.amount, -20)

        # Verify notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).latest('created_at')
        self.assertIn('abandoned', notification.message)

    def test_mutual_cancellation_refunds_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # 100 - 20 (deposit) + 20 (refund) = 100
        self.assertEqual(self.taker_profile.rewards, 100)

        # Verify ledger refund entry
        refund_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_refund')
        self.assertEqual(refund_ledger.amount, 20)

    @override_settings(TASK_COLLATERAL_PERCENTAGE=50)
    def test_custom_collateral_percentage_setting(self):
        self.assertEqual(self.task.collateral_required, 50)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.taker_collateral, 50)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50) # 100 - 50

    def test_exact_balance_boundary(self):
        # Set taker rewards to exactly 20
        self.taker_profile.rewards = 20
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taker_collateral, 20)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 0) # 20 - 20 = 0 (exact boundary)

    def test_complete_disputed_task_refunds_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Refresh task state from database
        self.task.refresh_from_db()

        # Taker raises dispute
        from basic.models import Dispute
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Issue with task")
        self.task.status = 'disputed'
        self.task.save()

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # 100 - 20 + 100 + 20 = 200
        self.assertEqual(self.taker_profile.rewards, 200)
