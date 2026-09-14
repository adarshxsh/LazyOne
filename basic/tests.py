from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.contrib.messages import get_messages
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, Conversation

class ViewStatusValidationAndDisputeCleanupTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.new_taker = User.objects.create_user(username='new_taker', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.new_taker, rewards=1000)

        self.task = Task.objects.create(
            title="Test Task",
            description="Task description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

        self.client = Client()

    def test_withdraw_dispute_rejected_if_dispute_not_open_or_task_not_disputed(self):
        # Case 1: Dispute is resolved and task is completed
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Issue", status='resolved')
        self.task.status = 'completed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot withdraw dispute" in str(m) for m in messages))

        # Case 2: Dispute is open but task status is in_progress
        dispute.status = 'open'
        dispute.save()
        self.task.status = 'in_progress'
        self.task.save()

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_success_when_open_and_disputed(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Issue", status='open')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_accept_cancellation_rejected_if_task_not_in_progress(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Dispute active")
        self.task.status = 'disputed'
        self.task.cancellation_requested = True
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot accept cancellation" in str(m) for m in messages))

    def test_accept_cancellation_deletes_dispute_atomically_when_returning_to_available(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Legacy dispute")
        self.task.status = 'in_progress'
        self.task.cancellation_requested = True
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_abandon_task_deletes_dispute_atomically_when_returning_to_available(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Legacy dispute")
        self.task.status = 'in_progress'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_new_taker_can_raise_dispute_on_reopened_task(self):
        # Taker 1 raises dispute / cancellation requested
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="First dispute")
        self.task.cancellation_requested = True
        self.task.status = 'in_progress'
        self.task.save()

        # Taker 1 accepts cancellation -> reverts to available and deletes dispute
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertFalse(hasattr(self.task, 'dispute'))

        # Taker 2 takes task
        self.client.login(username='new_taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.new_taker)

        # Taker 2 raises dispute
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Second dispute by new taker'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.new_taker)
        self.assertEqual(self.task.dispute.reason, 'Second dispute by new taker')
        self.assertRedirects(response, reverse('dispute_detail', args=[self.task.dispute.id]))
