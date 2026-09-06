from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, RewardLedger

class TaskStatusGuardAndDisputeCleanupTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker1_profile = UserProfile.objects.create(user=self.taker1, rewards=1000)

        self.taker2 = User.objects.create_user(username='taker2', password='password123')
        self.taker2_profile = UserProfile.objects.create(user=self.taker2, rewards=1000)

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker1 = Client()
        self.client_taker1.login(username='taker1', password='password123')

        self.client_taker2 = Client()
        self.client_taker2.login(username='taker2', password='password123')

        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

    def test_accept_cancellation_rejected_when_not_in_progress(self):
        # Taker1 takes the task
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker1 raises a dispute, putting task status into 'disputed'
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair expectations'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        # Taker1 attempts to accept cancellation while task is in 'disputed' status
        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Verify task remains disputed and dispute was NOT deleted
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=self.task).exists())

    def test_accept_cancellation_success_and_cleans_up_dispute(self):
        # Taker1 takes the task
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))

        # An orphaned/associated dispute record exists on the task
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Previous dispute")

        # Taker1 accepts cancellation while task is in 'in_progress' status
        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task should transition to 'available', taken_by cleared, and dispute record DELETED
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_reassigned_task_allows_new_dispute(self):
        # Taker1 takes task, dispute raised, cancellation accepted and dispute deleted
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Old dispute")
        self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))

        # Taker2 takes the newly available task
        self.client_taker2.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker2)

        # Taker2 raises a new dispute
        response = self.client_taker2.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'New issue by Taker2'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=self.task).exists())

        new_dispute = self.task.dispute
        self.assertEqual(new_dispute.raised_by, self.taker2)
        self.assertEqual(new_dispute.reason, 'New issue by Taker2')

    def test_withdraw_dispute_rejected_when_resolved_or_task_completed(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        self.task.refresh_from_db()
        dispute = self.task.dispute

        # Poster completes task (resolving the dispute and setting task status to 'completed')
        self.client_poster.get(reverse('complete_task', args=[self.task.id]))
        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # Taker1 attempts to withdraw the resolved dispute on completed task
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task state and dispute state must remain completed / resolved
        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

    def test_withdraw_dispute_rejected_when_task_cancelled(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = self.task.dispute

        # Task status changed to cancelled
        self.task.status = 'cancelled'
        self.task.save()

        # Taker1 attempts to withdraw dispute
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task status remains cancelled
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_success_when_open_and_disputed(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Valid issue'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute

        # Taker1 withdraws the open dispute
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task status transitions back to 'in_progress' and dispute is deleted
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
