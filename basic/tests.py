from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch, MagicMock
from datetime import timedelta
from .models import Task, Dispute, UserProfile
from .firebase_init import update_dispute_firestore

class DisputeFirestoreEventsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=500)
        UserProfile.objects.create(user=self.doer, rewards=100)

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=50,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=self.deadline,
            status='in_progress'
        )

    @patch('basic.views.dispute.update_dispute_firestore')
    def test_raise_dispute_triggers_firestore_event(self, mock_update_firestore):
        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})

        self.task.refresh_from_db()
        dispute = Dispute.objects.get(task=self.task)

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.reason, 'Unfair requirement')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='open',
            event_type='dispute_raised',
            raised_by_username='doer'
        )

    @patch('basic.views.dispute.update_dispute_firestore')
    def test_withdraw_dispute_triggers_firestore_event(self, mock_update_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Need to withdraw')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='withdrawn',
            event_type='dispute_withdrawn',
            raised_by_username='doer'
        )

    @patch('basic.views.tasks.update_dispute_firestore')
    def test_complete_task_resolves_dispute_triggers_firestore_event(self, mock_update_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Resolving this')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='resolved',
            event_type='dispute_resolved',
            raised_by_username='doer'
        )

    @patch('firebase_admin._apps', True)
    @patch('firebase_admin.firestore.client')
    def test_update_dispute_firestore_helper_executes_set(self, mock_firestore_client):
        mock_db = MagicMock()
        mock_doc = MagicMock()
        mock_firestore_client.return_value = mock_db
        mock_db.collection.return_value.doc.return_value = mock_doc

        update_dispute_firestore(
            dispute_id=1,
            task_id=10,
            status='open',
            event_type='dispute_raised',
            raised_by_username='doer'
        )

        mock_db.collection.assert_called_once_with('disputes')
        mock_db.collection().doc.assert_called_once_with('1')
        mock_doc.set.assert_called_once()
        args, kwargs = mock_doc.set.call_args
        self.assertEqual(args[0]['dispute_id'], 1)
        self.assertEqual(args[0]['task_id'], 10)
        self.assertEqual(args[0]['status'], 'open')
        self.assertEqual(args[0]['event_type'], 'dispute_raised')
        self.assertEqual(args[0]['raised_by'], 'doer')
        self.assertTrue(kwargs.get('merge'))

    @patch('basic.firebase_init.initialize_firebase', side_effect=Exception("Firebase timeout"))
    def test_update_dispute_firestore_handles_exceptions_gracefully(self, mock_init):
        # Should catch exception and not raise error
        try:
            update_dispute_firestore(
                dispute_id=1,
                task_id=10,
                status='open',
                event_type='dispute_raised',
                raised_by_username='doer'
            )
        except Exception as e:
            self.fail(f"update_dispute_firestore raised an unexpected exception: {e}")
