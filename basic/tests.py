from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, Conversation

class DisputeSystemTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.admin_user = User.objects.create_superuser(username='admin', password='password123', is_staff=True)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        self.client_admin = Client()
        self.client_admin.login(username='admin', password='password123')

    def test_poster_can_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Taker stopped responding'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.reason, 'Taker stopped responding')

        # Check notification target is taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn('poster has raised a dispute', notification.message)

    def test_taker_can_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker.post(url, {'reason': 'Poster provided invalid requirements'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        self.assertEqual(self.task.dispute.status, 'open')

        # Check notification target is poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker has raised a dispute', notification.message)

    def test_unauthorized_user_cannot_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_other.post(url, {'reason': 'Random user trying to dispute'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_if_task_not_in_progress(self):
        self.task.status = 'available'
        self.task.save()

        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Task not started'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_withdraw_dispute_sets_status_withdrawn_and_retains_row(self):
        # Poster raises dispute
        url_raise = reverse('raise_dispute', args=[self.task.id])
        self.client_poster.post(url_raise, {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster withdraws dispute
        url_withdraw = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_poster.post(url_withdraw)

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(dispute.status, 'withdrawn')
        # Database row must NOT be deleted
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        # Check notification target is taker
        notification = Notification.objects.filter(recipient=self.taker).order_by('-created_at').first()
        self.assertIsNotNone(notification)
        self.assertIn('poster has withdrawn the dispute', notification.message)

    def test_non_initiator_cannot_withdraw_dispute(self):
        # Poster raises dispute
        url_raise = reverse('raise_dispute', args=[self.task.id])
        self.client_poster.post(url_raise, {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Taker attempts to withdraw dispute
        url_withdraw = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_taker.post(url_withdraw)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_admin_staff_can_withdraw_dispute(self):
        # Taker raises dispute
        url_raise = reverse('raise_dispute', args=[self.task.id])
        self.client_taker.post(url_raise, {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Admin withdraws dispute
        url_withdraw = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_admin.post(url_withdraw)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

    def test_re_raising_dispute_after_withdrawal(self):
        # Taker raises and withdraws
        url_raise = reverse('raise_dispute', args=[self.task.id])
        self.client_taker.post(url_raise, {'reason': 'First dispute'})
        dispute = Dispute.objects.get(task=self.task)
        url_withdraw = reverse('withdraw_dispute', args=[dispute.id])
        self.client_taker.post(url_withdraw)

        # Now Poster raises dispute on the same task
        response = self.client_poster.post(url_raise, {'reason': 'Second dispute by poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Second dispute by poster')
        self.assertEqual(Dispute.objects.count(), 1)

    def test_my_tasks_ui_buttons(self):
        # 1. When in_progress, poster should see Raise Dispute button
        response = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response, f"openDisputeModal('{self.task.id}')")

        # 2. Poster raises dispute
        url_raise = reverse('raise_dispute', args=[self.task.id])
        self.client_poster.post(url_raise, {'reason': 'Dispute by poster'})

        # Poster should see Withdraw Dispute button
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, "Withdraw Dispute")

        # Taker should see dispute details but NOT Withdraw Dispute button
        response_taker = self.client_taker.get(reverse('my_tasks'))
        self.assertContains(response_taker, "This task is in dispute")
        self.assertNotContains(response_taker, "Withdraw Dispute")
