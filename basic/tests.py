from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation
from django.utils import timezone
from datetime import timedelta

class DisputeInitiationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.other_user, rewards=1000)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_poster_can_raise_dispute(self):
        client = Client()
        client.login(username='poster', password='password123')
        
        response = client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Taker is unresponsive'}
        )
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Taker is unresponsive')
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

        # Check notification sent to taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn("poster has raised a dispute", notification.message)

    def test_taker_can_raise_dispute(self):
        client = Client()
        client.login(username='taker', password='password123')

        response = client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster demands extra work'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        self.assertEqual(self.task.dispute.reason, 'Poster demands extra work')
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn("taker has raised a dispute", notification.message)

    def test_third_party_cannot_raise_dispute(self):
        client = Client()
        client.login(username='other', password='password123')

        response = client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Malicious attempt'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_cannot_raise_dispute_on_available_task(self):
        self.task.status = 'available'
        self.task.taken_by = None
        self.task.save()

        client = Client()
        client.login(username='poster', password='password123')

        response = client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Task not taken yet'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_withdraw_dispute_by_initiator(self):
        # Poster raises dispute
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue')
        self.task.status = 'disputed'
        self.task.save()

        client = Client()
        client.login(username='poster', password='password123')

        response = client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        # Check taker received withdrawal notification
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn("poster has withdrawn the dispute", notification.message)

    def test_withdraw_dispute_blocked_for_non_initiator(self):
        # Poster raises dispute
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue')
        self.task.status = 'disputed'
        self.task.save()

        client = Client()
        # Taker tries to withdraw poster's dispute
        client.login(username='taker', password='password123')

        response = client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertRedirects(response, reverse('my_tasks'))

    def test_my_tasks_ui_dispute_buttons(self):
        client = Client()
        client.login(username='poster', password='password123')

        response = client.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Raise Dispute')

        # Create dispute by poster
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue')
        self.task.status = 'disputed'
        self.task.save()

        # Poster sees Withdraw Dispute button
        response = client.get(reverse('my_tasks'))
        self.assertContains(response, 'You have raised a dispute.')
        self.assertContains(response, 'Withdraw Dispute')

        # Taker does NOT see Withdraw Dispute button
        client.login(username='taker', password='password123')
        response = client.get(reverse('my_tasks'))
        self.assertContains(response, 'This task is in dispute.')
        self.assertNotContains(response, 'Withdraw Dispute')
