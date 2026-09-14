from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, Notification, Conversation

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PosterDisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='admin', password='password123', is_staff=True)

        UserProfile.objects.get_or_create(user=self.poster, rewards=1500)
        UserProfile.objects.get_or_create(user=self.taker, rewards=1500)
        UserProfile.objects.get_or_create(user=self.other_user, rewards=1500)
        UserProfile.objects.get_or_create(user=self.staff_user, rewards=1500)

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
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker stopped responding'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        dispute = self.task.dispute
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.reason, 'Taker stopped responding')

        # Check counterparty notification
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has raised a dispute', notification.message)

    def test_taker_can_raise_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster demands extra work'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.status, 'open')

        # Check counterparty notification
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('taker has raised a dispute', notification.message)

    def test_unauthorized_user_cannot_raise_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Invalid dispute'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_if_task_not_in_progress(self):
        available_task = Task.objects.create(
            title='Available Task', description='Desc', reward=50, posted_by=self.poster, status='available'
        )
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[available_task.id]), {'reason': 'Not in progress'})

        available_task.refresh_from_db()
        self.assertEqual(available_task.status, 'available')
        self.assertFalse(hasattr(available_task, 'dispute'))

    def test_withdraw_dispute_retains_row_and_transitions_status(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)

        # Taker withdraws dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        # Task restored to in_progress
        self.assertEqual(self.task.status, 'in_progress')
        # Dispute status updated to withdrawn
        self.assertEqual(dispute.status, 'withdrawn')
        # Dispute record is retained in DB (not deleted)
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        # Notification sent to counterparty (poster)
        notifications = Notification.objects.filter(recipient=self.poster)
        self.assertTrue(notifications.filter(message__contains='withdrawn').exists())

    def test_poster_can_withdraw_own_dispute(self):
        # Poster raises dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster withdraws dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(dispute.status, 'withdrawn')

        # Notification sent to counterparty (taker)
        notification = Notification.objects.filter(recipient=self.taker, message__contains='withdrawn').first()
        self.assertIsNotNone(notification)

    def test_unauthorized_user_cannot_withdraw_dispute(self):
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

    def test_staff_user_can_withdraw_dispute(self):
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster dispute'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')

    def test_reactivate_withdrawn_dispute(self):
        # 1. Poster raises dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Initial dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # 2. Poster withdraws dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')

        # 3. Taker raises new dispute on same task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Second dispute'})

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.reason, 'Second dispute')

    def test_task_completion_resolves_open_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
