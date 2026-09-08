from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import Task, Dispute, Notification, UserProfile, Conversation

class DisputeInitiationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other_user', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.other_user, rewards=1000)

        self.assigned_task = Task.objects.create(
            title='Test Task Assigned',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.assigned_task)
        self.conversation.participants.add(self.poster, self.taker)

        self.unassigned_task = Task.objects.create(
            title='Test Task Unassigned',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            status='available',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client = Client()

    def test_poster_can_raise_dispute_and_notification_routed_to_taker(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.assigned_task.id]),
            {'reason': 'Taker stopped responding'}
        )
        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.assigned_task)
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Taker stopped responding')

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        notifications = Notification.objects.filter(recipient=self.taker)
        self.assertEqual(notifications.count(), 1)
        self.assertIn("poster has raised a dispute", notifications.first().message)

        poster_notifications = Notification.objects.filter(recipient=self.poster)
        self.assertEqual(poster_notifications.count(), 0)

    def test_poster_can_withdraw_dispute_and_notification_routed_to_taker(self):
        dispute = Dispute.objects.create(
            task=self.assigned_task,
            raised_by=self.poster,
            reason='Poster dispute'
        )
        self.assigned_task.status = 'disputed'
        self.assigned_task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        self.assertRedirects(response, reverse('my_tasks'))

        notifications = Notification.objects.filter(recipient=self.taker)
        self.assertEqual(notifications.count(), 1)
        self.assertIn("poster has withdrawn the dispute", notifications.first().message)

        poster_notifications = Notification.objects.filter(recipient=self.poster)
        self.assertEqual(poster_notifications.count(), 0)

    def test_taker_can_raise_and_withdraw_dispute_routed_to_poster(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.assigned_task.id]),
            {'reason': 'Poster demands extra work'}
        )
        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.assigned_task)
        self.assertEqual(dispute.raised_by, self.taker)

        poster_notifications = Notification.objects.filter(recipient=self.poster)
        self.assertEqual(poster_notifications.count(), 1)

        taker_notifications = Notification.objects.filter(recipient=self.taker)
        self.assertEqual(taker_notifications.count(), 0)

        # Withdraw
        response_withdraw = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.status, 'in_progress')

        poster_notifications_after = Notification.objects.filter(recipient=self.poster)
        self.assertEqual(poster_notifications_after.count(), 2)
        taker_notifications_after = Notification.objects.filter(recipient=self.taker)
        self.assertEqual(taker_notifications_after.count(), 0)

    def test_poster_initiated_dispute_appears_in_community_review_feed(self):
        Dispute.objects.create(
            task=self.assigned_task,
            raised_by=self.poster,
            reason='Stalled task'
        )
        self.assigned_task.status = 'disputed'
        self.assigned_task.save()

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('disputed_tasks', response.context)
        self.assertIn(self.assigned_task, response.context['disputed_tasks'])

    def test_unassigned_task_cannot_be_disputed(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.unassigned_task.id]),
            {'reason': 'Nobody took it'}
        )
        self.unassigned_task.refresh_from_db()
        self.assertEqual(self.unassigned_task.status, 'available')
        self.assertFalse(Dispute.objects.filter(task=self.unassigned_task).exists())

    def test_non_participant_cannot_raise_dispute(self):
        self.client.login(username='other_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.assigned_task.id]),
            {'reason': 'Third party dispute'}
        )
        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.assigned_task).exists())

    def test_my_tasks_renders_dispute_trigger_for_poster(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"openDisputeModal('{self.assigned_task.id}')")

