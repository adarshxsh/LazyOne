from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation

class BilateralDisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.other_user)

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

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

    def test_poster_can_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Taker is non-responsive'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Taker is non-responsive')
        
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        
        # Check notification routing (sent to taker)
        noti = Notification.objects.filter(recipient=self.taker).latest('created_at')
        self.assertIn('poster has raised a dispute', noti.message)
        self.assertFalse(Notification.objects.filter(recipient=self.poster).exists())

    def test_taker_can_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker.post(url, {'reason': 'Poster is unreasonable'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.reason, 'Poster is unreasonable')
        
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        
        # Check notification routing (sent to poster)
        noti = Notification.objects.filter(recipient=self.poster).latest('created_at')
        self.assertIn('taker has raised a dispute', noti.message)
        self.assertFalse(Notification.objects.filter(recipient=self.taker).exists())

    def test_unauthorized_user_cannot_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_other.post(url, {'reason': 'Random intrusion'}, follow=True)
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_cannot_raise_dispute_on_non_in_progress_task(self):
        self.task.status = 'available'
        self.task.save()
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Not started yet'}, follow=True)
        
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_withdraw_dispute_by_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue raised')
        self.task.status = 'disputed'
        self.task.save()

        url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_poster.post(url)
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        
        # Notification sent to taker, not poster
        noti = Notification.objects.filter(recipient=self.taker).latest('created_at')
        self.assertIn('poster has withdrawn the dispute', noti.message)

    def test_withdraw_dispute_by_taker(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue raised')
        self.task.status = 'disputed'
        self.task.save()

        url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client_taker.post(url)
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        
        # Notification sent to poster, not taker
        noti = Notification.objects.filter(recipient=self.poster).latest('created_at')
        self.assertIn('taker has withdrawn the dispute', noti.message)

    def test_non_initiator_cannot_withdraw_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue raised')
        self.task.status = 'disputed'
        self.task.save()

        url = reverse('withdraw_dispute', args=[dispute.id])
        # Taker attempts to withdraw poster's dispute
        response = self.client_taker.post(url)
        self.assertEqual(response.status_code, 404)
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_ui_posted_tasks_raise_dispute_button_rendered(self):
        response = self.client_poster.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Raise Dispute')

    def test_ui_posted_tasks_withdraw_dispute_for_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_poster.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'You have raised a dispute')

    def test_ui_posted_tasks_counterparty_dispute_for_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_poster.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'This task is in dispute')

    def test_ui_taken_tasks_withdraw_dispute_for_taker(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_taker.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'You have raised a dispute')

    def test_ui_taken_tasks_counterparty_dispute_for_taker(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_taker.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'The poster has raised a dispute')

    def test_dispute_detail_displays_role(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_poster.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'poster (Poster)')
