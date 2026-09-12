from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, Conversation

class DisputeStatusGuardsTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.new_taker = User.objects.create_user(username='new_taker', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.new_taker, rewards=1000)

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_new_taker = Client()
        self.client_new_taker.login(username='new_taker', password='password123')

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_withdraw_dispute_valid(self):
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        response = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_resolved_or_completed_task_fails(self):
        self.task.status = 'completed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Resolved issue', status='resolved')

        response = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertContains(response, "Cannot withdraw a dispute that is resolved or on a task that is not currently disputed")

    def test_accept_cancellation_disputed_task_fails(self):
        self.task.status = 'disputed'
        self.task.cancellation_requested = True
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')

        response = self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertContains(response, "Cannot accept cancellation for a task that is not in progress.")

    def test_accept_cancellation_cleans_orphaned_dispute(self):
        self.task.status = 'in_progress'
        self.task.cancellation_requested = True
        self.task.save()
        # Create an orphaned dispute record if any existed
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Old issue', status='resolved')

        response = self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_abandon_task_cleans_orphaned_dispute(self):
        self.task.status = 'in_progress'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Old issue', status='resolved')

        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_raise_dispute_clears_old_resolved_dispute_for_new_taker(self):
        # Old dispute on task
        old_dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Old issue', status='resolved')
        
        # Reset task to in_progress with new_taker
        self.task.status = 'in_progress'
        self.task.taken_by = self.new_taker
        self.task.save()

        response = self.client_new_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'New taker issue'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.new_taker)
        self.assertEqual(self.task.dispute.reason, 'New taker issue')
        self.assertNotEqual(self.task.dispute.id, old_dispute.id)

    def test_raise_dispute_open_dispute_redirects_to_detail(self):
        self.task.status = 'disputed'
        self.task.save()
        open_dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Open issue', status='open')

        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Another issue'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[open_dispute.id]))
