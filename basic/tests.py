from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation

class DisputeHandlingTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.create(user=self.poster)
        UserProfile.objects.create(user=self.taker)
        UserProfile.objects.create(user=self.other_user)

        self.task = Task.objects.create(
            title='Test Task',
            description='Task Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_poster_can_raise_dispute(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker non-performance'})
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.reason, 'Taker non-performance')
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

        notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notif)
        self.assertIn('poster has raised a dispute', notif.message)

    def test_taker_can_raise_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster demands extra work'})
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.reason, 'Poster demands extra work')
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

        notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notif)
        self.assertIn('taker has raised a dispute', notif.message)

    def test_unauthorized_user_cannot_raise_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Invalid request'})
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_cannot_raise_dispute_if_task_not_in_progress(self):
        self.task.status = 'available'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Not in progress'})
        self.assertRedirects(response, reverse('my_tasks'))
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_attempt_raise_dispute_with_open_dispute_redirects_to_detail(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Initial dispute', status='open')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Second dispute'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_withdraw_dispute_sets_status_withdrawn_and_preserves_row(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason', status='open')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notif)
        self.assertIn('taker has withdrawn the dispute', notif.message)

    def test_poster_can_withdraw_own_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute', status='open')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notif)
        self.assertIn('poster has withdrawn the dispute', notif.message)

    def test_non_author_cannot_withdraw_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute', status='open')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertEqual(response.status_code, 404)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

    def test_reopen_withdrawn_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='First dispute', status='withdrawn')
        self.task.status = 'in_progress'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Second dispute by poster'})
        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Second dispute by poster')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_dispute_detail_view_withdrawn_status(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Some dispute', status='withdrawn')

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Withdrawn')

    def test_unauthorized_user_cannot_view_dispute_detail(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Some dispute', status='open')

        self.client.login(username='other', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))
