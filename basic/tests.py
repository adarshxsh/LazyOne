from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation

class DisputePermissionsTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.create(user=self.poster)
        UserProfile.objects.create(user=self.taker)
        UserProfile.objects.create(user=self.other_user)

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

        self.client = Client()

    def test_poster_can_raise_dispute_on_in_progress_task(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker stopped working'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Taker stopped working')

        # Check notification sent to taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn('poster has raised a dispute', notification.message)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_taker_can_raise_dispute_on_in_progress_task(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster requirements changed'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker has raised a dispute', notification.message)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_unauthorized_user_cannot_raise_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Malicious dispute'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_cannot_raise_dispute_on_non_in_progress_task(self):
        self.task.status = 'available'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task is available'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_dispute_requires_reason(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': ''})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_poster_can_withdraw_initiated_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Misunderstanding resolved')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Check notification sent to taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn('poster has withdrawn the dispute', notification.message)
        self.assertRedirects(response, reverse('my_tasks'))

    def test_taker_can_withdraw_initiated_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Misunderstanding resolved')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker has withdrawn the dispute', notification.message)
        self.assertRedirects(response, reverse('my_tasks'))

    def test_my_tasks_ui_renders_raise_dispute_for_poster(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"openDisputeModal('{self.task.id}')")
