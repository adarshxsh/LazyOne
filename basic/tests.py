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

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

    def test_abandon_task_deducts_penalty_and_logs_ledger(self):
        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', kwargs={'task_id': self.task.id})

        response = self.client.get(url, follow=True)

        self.assertRedirects(response, reverse('my_tasks'))

        # Verify task status and assignment reset
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Verify point deduction on user profile
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1450)

        # Verify RewardLedger entry creation
        ledger_entry = RewardLedger.objects.get(user=self.taker, task=self.task)
        self.assertEqual(ledger_entry.transaction_type, 'task_abandonment')
        self.assertEqual(ledger_entry.amount, -50)
        self.assertIn("Penalty for abandoning task", ledger_entry.description)

        # Verify Notification creation for poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn("has abandoned your task", notification.message)

        # Verify immediate feedback message
        messages = list(response.context['messages'])
        self.assertTrue(any("penalty of 50 points has been deducted" in str(m) for m in messages))

    def test_abandonment_appears_on_rewards_dashboard(self):
        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', kwargs={'task_id': self.task.id})
        self.client.get(url)

        # Visit rewards page
        rewards_url = reverse('rewards')
        response = self.client.get(rewards_url)

        self.assertEqual(response.status_code, 200)

        # Verify transactions in context
        transactions = list(response.context['all_transactions'])
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].transaction_type, 'task_abandonment')
        self.assertEqual(transactions[0].amount, -50)
        self.assertContains(response, "-50")
        self.assertContains(response, "Penalty for abandoning task")

    def test_cannot_abandon_unassigned_task(self):
        other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=other_user, rewards=1500)

        self.client.login(username='other', password='password123')
        url = reverse('abandon_task', kwargs={'task_id': self.task.id})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_cannot_abandon_completed_task(self):
        self.task.status = 'completed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        url = reverse('abandon_task', kwargs={'task_id': self.task.id})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

