from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, UserProfile, RewardLedger, Notification

class TaskAbandonmentPenaltyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        
        # UserProfiles are auto-created or manually set up
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.taker_profile.rewards = 1500
        self.taker_profile.save()

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

    def test_abandon_task_penalty_deduction_and_ledger(self):
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.assertEqual(response.status_code, 200)

        # Check taker reward balance deducted by 20% (20 points)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1480)

        # Check task status reset
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Check RewardLedger entry
        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_abandonment').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)
        self.assertEqual(ledger_entry.transaction_type, 'task_abandonment')

        # Check notification to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('abandoned', notification.message)

    def test_abandon_task_minimum_penalty(self):
        # Task reward is 4, 20% is 0.8 => minimum penalty should be 1
        small_task = Task.objects.create(
            title='Small Task',
            description='Small Description',
            reward=4,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('abandon_task', args=[small_task.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1499)

        ledger_entry = RewardLedger.objects.get(user=self.taker, task=small_task, transaction_type='task_abandonment')
        self.assertEqual(ledger_entry.amount, -1)

    def test_abandon_task_low_balance_safeguard(self):
        # Taker profile has 5 points, task reward is 100 (penalty 20)
        self.taker_profile.rewards = 5
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('abandon_task', args=[self.task.id]))

        self.taker_profile.refresh_from_db()
        # Balance safeguards against negative underflow -> capped at 0
        self.assertEqual(self.taker_profile.rewards, 0)

        # Full penalty recorded in audit ledger
        ledger_entry = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='task_abandonment')
        self.assertEqual(ledger_entry.amount, -20)

    def test_rewards_view_shows_abandonment_transaction(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('abandon_task', args=[self.task.id]))

        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Penalty for abandoning task')
        self.assertContains(response, '-20')

