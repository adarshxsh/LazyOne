from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import Task, Dispute, UserProfile, RewardLedger

class TaskDisputeStatusCheckTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker2 = User.objects.create_user(username='taker2', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker1, rewards=1000)
        UserProfile.objects.create(user=self.taker2, rewards=1000)

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
            status='in_progress',
            taken_by=self.taker1,
            cancellation_requested=True
        )

    def test_accept_cancellation_success_and_removes_dispute(self):
        # Attach a dispute to the task
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker1,
            reason="Old dispute",
            status="resolved"
        )
        url = reverse('accept_cancellation', args=[self.task.id])
        response = self.client_taker1.get(url)

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_accept_cancellation_returns_404_if_not_in_progress(self):
        self.task.status = 'disputed'
        self.task.save()

        url = reverse('accept_cancellation', args=[self.task.id])
        response = self.client_taker1.get(url)

        self.assertEqual(response.status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_withdraw_dispute_success(self):
        self.task.status = 'disputed'
        self.task.cancellation_requested = False
        self.task.save()

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker1,
            reason="I want to dispute this",
            status="open"
        )

        url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_taker1.post(url)

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_returns_404_if_dispute_not_open_or_task_not_disputed(self):
        # Case 1: dispute is resolved, task is completed
        self.task.status = 'completed'
        self.task.save()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker1,
            reason="Resolved dispute",
            status="resolved"
        )

        url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_taker1.post(url)

        self.assertEqual(response.status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_raise_dispute_with_existing_resolved_dispute_allows_new_taker(self):
        # Task re-assigned to taker2
        self.task.status = 'in_progress'
        self.task.taken_by = self.taker2
        self.task.cancellation_requested = False
        self.task.save()

        # Stale resolved dispute from previous taker1
        old_dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker1,
            reason="Old resolved dispute",
            status="resolved"
        )

        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker2.post(url, {'reason': 'New taker issue'})

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertFalse(Dispute.objects.filter(id=old_dispute.id).exists())

        new_dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(new_dispute.raised_by, self.taker2)
        self.assertEqual(new_dispute.reason, 'New taker issue')
        self.assertEqual(new_dispute.status, 'open')

    def test_raise_dispute_open_dispute_redirects_to_detail(self):
        self.task.status = 'disputed'
        self.task.cancellation_requested = False
        self.task.save()

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker1,
            reason="Already open dispute",
            status="open"
        )

        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker1.post(url, {'reason': 'Another reason'})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dispute_detail', args=[dispute.id]), response.url)
