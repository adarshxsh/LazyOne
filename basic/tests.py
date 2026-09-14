from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from unittest.mock import patch, MagicMock
from basic.models import Task, Dispute, Notification, UserProfile
from basic.views.dispute import sync_dispute_to_firestore

class DisputeFirestoreSyncTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.claimer = User.objects.create_user(username='claimer', password='password123')
        
        # Ensure user profiles exist
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.claimer)

        self.task = Task.objects.create(
            title='Test Task for Dispute',
            description='Detailed description',
            reward=10,
            posted_by=self.poster,
            taken_by=self.claimer,
            status='in_progress'
        )

    @patch('basic.views.dispute.firestore')
    @patch('basic.views.dispute.firebase_admin')
    @patch('basic.views.dispute.initialize_firebase')
    def test_raise_dispute_syncs_to_firestore(self, mock_init_fb, mock_fb_admin, mock_firestore):
        mock_fb_admin._apps = ['default_app']
        mock_db = MagicMock()
        mock_doc_ref = MagicMock()
        mock_firestore.client.return_value = mock_db
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        self.client.login(username='claimer', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not completed as expected'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        
        dispute = self.task.dispute
        self.assertEqual(dispute.reason, 'Task not completed as expected')
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Verify notification created for poster
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

        # Verify Firestore set called
        mock_db.collection.assert_called_with('disputes')
        mock_db.collection.return_value.document.assert_called_with(str(dispute.id))
        mock_doc_ref.set.assert_called_once()
        args, kwargs = mock_doc_ref.set.call_args
        data = args[0]
        self.assertEqual(data['id'], dispute.id)
        self.assertEqual(data['task_id'], self.task.id)
        self.assertEqual(data['raised_by'], 'claimer')
        self.assertEqual(data['status'], 'open')

    @patch('basic.views.dispute.firestore')
    @patch('basic.views.dispute.firebase_admin')
    @patch('basic.views.dispute.initialize_firebase')
    def test_withdraw_dispute_syncs_and_deletes_firestore_doc(self, mock_init_fb, mock_fb_admin, mock_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.claimer, reason='Initial reason')
        self.task.status = 'disputed'
        self.task.save()

        mock_fb_admin._apps = ['default_app']
        mock_db = MagicMock()
        mock_doc_ref = MagicMock()
        mock_firestore.client.return_value = mock_db
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        self.client.login(username='claimer', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('my_tasks'))

        # Verify Firestore delete called
        mock_db.collection.assert_called_with('disputes')
        mock_doc_ref.delete.assert_called_once()

    @patch('basic.views.dispute.initialize_firebase')
    @patch('basic.views.dispute.firebase_admin')
    def test_firestore_sync_fails_gracefully(self, mock_fb_admin, mock_init_fb):
        # Simulate uninitialized Firebase (e.g., missing credentials)
        mock_fb_admin._apps = []

        self.client.login(username='claimer', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Reason without firebase'})

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    @patch('basic.views.dispute.firestore')
    @patch('basic.views.dispute.firebase_admin')
    @patch('basic.views.dispute.initialize_firebase')
    def test_complete_task_resolves_dispute_syncs_firestore(self, mock_init_fb, mock_fb_admin, mock_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.claimer, reason='Dispute to resolve')
        self.task.status = 'disputed'
        self.task.save()

        mock_fb_admin._apps = ['default_app']
        mock_db = MagicMock()
        mock_doc_ref = MagicMock()
        mock_firestore.client.return_value = mock_db
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(response.status_code, 302)

        mock_db.collection.assert_called_with('disputes')
        mock_doc_ref.set.assert_called_once()
        args, kwargs = mock_doc_ref.set.call_args
        data = args[0]
        self.assertEqual(data['status'], 'resolved')

    def test_dispute_detail_template_renders_firebase_listeners(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.claimer, reason='Detail view test')
        self.client.login(username='claimer', password='password123')

        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        self.assertIn('firebase-firestore.js', content)
        self.assertIn('db.collection(\'disputes\').doc(disputeId).onSnapshot', content)
        self.assertIn('dispute-status-badge', content)
        self.assertIn('withdraw-dispute-btn', content)

