from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import Task, Dispute, Notification, UserProfile, Conversation
from django.urls import reverse

class DisputeAuthAndSoftWithdrawalTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.stranger = User.objects.create_user(username='stranger', password='password123')

        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.stranger)

        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_poster_can_raise_dispute(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster reason'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        
        dispute = self.task.dispute
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Poster reason')

        # Notification to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster', notification.message)
        self.assertIn('raised a dispute', notification.message)

    def test_taker_can_raise_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker reason'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = self.task.dispute
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.reason, 'Taker reason')

        # Notification to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('taker', notification.message)
        self.assertIn('raised a dispute', notification.message)

    def test_non_participant_cannot_raise_dispute(self):
        self.client.login(username='stranger', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Stranger reason'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_if_task_not_in_progress(self):
        self.task.status = 'available'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Reason'})

        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_poster_withdraw_dispute_soft_deletion(self):
        # Poster raises dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster reason'})
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        # Clear notifications from setup
        Notification.objects.all().delete()

        # Poster withdraws dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        # Refresh dispute and task
        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Dispute record retained with status 'withdrawn'
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertEqual(dispute.status, 'withdrawn')

        # Task status returned to 'in_progress'
        self.assertEqual(self.task.status, 'in_progress')

        # Notification sent to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('withdrawn', notification.message)

    def test_taker_withdraw_dispute_soft_deletion(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker reason'})
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        # Clear notifications
        Notification.objects.all().delete()

        # Taker withdraws dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Dispute retained, status 'withdrawn', task 'in_progress'
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertEqual(dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')

        # Notification sent to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('withdrawn', notification.message)

    def test_taker_cannot_withdraw_dispute_raised_by_poster(self):
        # Poster raises dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Taker tries to withdraw
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

    def test_poster_cannot_withdraw_dispute_raised_by_taker(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster tries to withdraw
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

    def test_re_raise_dispute_after_withdrawal(self):
        # Taker raises and withdraws
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker reason'})
        dispute = Dispute.objects.get(task=self.task)
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Now Poster raises dispute on same task
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster new reason'})

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Poster new reason')
