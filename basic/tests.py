from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation


class DisputeSymmetricPermissionsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.other_user)

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
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Taker stopped responding')

        # Check notification sent to taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn('poster', notification.message)
        self.assertIn('raised a dispute', notification.message)

        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_taker_can_raise_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster demands extra work'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker', notification.message)
        self.assertIn('raised a dispute', notification.message)

        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_non_participant_cannot_raise_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unrelated user'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_for_non_in_progress_task(self):
        self.task.status = 'available'
        self.task.taken_by = None
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Invalid status'})

        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_raise_dispute_when_dispute_already_exists_redirects(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Existing dispute')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Another dispute'})

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_poster_can_withdraw_own_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Notification to taker
        notification = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notification)
        self.assertIn('withdrawn', notification.message)

    def test_taker_can_withdraw_own_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Notification to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('withdrawn', notification.message)

    def test_user_cannot_withdraw_other_users_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_non_participant_cannot_view_dispute_detail(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        
        self.client.login(username='other', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_my_tasks_ui_rendering_dispute_controls(self):
        # Poster view for in_progress task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, 'Raise Dispute')

        # Create dispute by poster
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        # Poster sees Withdraw Dispute
        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'You have raised a dispute.')

        # Taker sees View Dispute, but not Withdraw Dispute
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, 'View Dispute')
        self.assertNotContains(response, 'Withdraw Dispute')
