from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Task, Dispute, Notification, UserProfile, Conversation

class SymmetricDisputeTests(TestCase):
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

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

    def test_poster_can_raise_dispute(self):
        response = self.client_poster.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Taker stopped responding'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Taker stopped responding')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Check notification delivered to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn("poster has raised a dispute", notification.message)

    def test_taker_can_raise_dispute(self):
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster demands extra work'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.reason, 'Poster demands extra work')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Check notification delivered to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn("taker has raised a dispute", notification.message)

    def test_unauthorized_user_cannot_raise_dispute(self):
        response = self.client_other.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'I am not involved'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertRedirects(response, reverse('my_tasks'))

    def test_cannot_raise_dispute_for_non_in_progress_task(self):
        available_task = Task.objects.create(
            title='Available Task',
            description='Desc',
            reward=50,
            posted_by=self.poster,
            status='available'
        )
        response = self.client_poster.post(
            reverse('raise_dispute', args=[available_task.id]),
            {'reason': 'No taker yet'}
        )
        available_task.refresh_from_db()
        self.assertEqual(available_task.status, 'available')
        self.assertFalse(Dispute.objects.filter(task=available_task).exists())

    def test_poster_can_withdraw_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_poster.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        # Check notification sent to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn("poster has withdrawn the dispute", notification.message)

    def test_taker_can_withdraw_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        # Check notification sent to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn("taker has withdrawn the dispute", notification.message)

    def test_user_cannot_withdraw_dispute_raised_by_other(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        response = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_my_tasks_template_renders_symmetric_triggers(self):
        # Poster view for in-progress task should have Raise Dispute button
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, "Raise Dispute")

        # Create dispute by poster
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Issue')
        self.task.status = 'disputed'
        self.task.save()

        # Poster view for disputed task raised by poster should have Withdraw Dispute button
        response_poster_disputed = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster_disputed, "Withdraw Dispute")
        self.assertContains(response_poster_disputed, "View Dispute")

        # Taker view for disputed task raised by poster should have View Dispute but NOT Withdraw Dispute button
        response_taker_disputed = self.client_taker.get(reverse('my_tasks'))
        self.assertContains(response_taker_disputed, "View Dispute")
        self.assertNotContains(response_taker_disputed, "Withdraw Dispute")
