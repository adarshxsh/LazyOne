from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from django.contrib.messages import get_messages
from basic.models import Task, UserProfile, Dispute, Conversation


class TaskStateGuardTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=500)

        future_deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=future_deadline,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)


    def test_complete_task_success(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

    def test_complete_task_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='poster', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('complete_task', args=[self.task.id]))
            self.assertIn("attempted to complete disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))

    def test_abandon_task_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='taker', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('abandon_task', args=[self.task.id]))
            self.assertIn("attempted to abandon disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))

    def test_cancel_task_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='poster', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('cancel_task', args=[self.task.id]))
            self.assertIn("attempted to cancel disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))

    def test_withdraw_dispute_success_future_deadline(self):
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_request_cancellation_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='poster', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('request_cancellation', args=[self.task.id]))
            self.assertIn("attempted to request cancellation for disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))

    def test_accept_cancellation_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.cancellation_requested = True
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='taker', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))
            self.assertIn("attempted to accept cancellation for disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))

    def test_withdraw_dispute_rejected_expired_deadline(self):
        self.task.status = 'disputed'
        self.task.deadline = timezone.now() - timedelta(hours=1)
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        self.client.login(username='taker', password='password123')
        with self.assertLogs('basic.views.dispute', level='WARNING') as cm:
            response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
            self.assertIn("after deadline passed", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot withdraw dispute because the task deadline has passed." in m.message for m in messages))

    def test_take_task_rejected_when_disputed(self):
        self.task.status = 'disputed'
        self.task.save()
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Issue with task')

        other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=other_user, rewards=500)
        self.client.login(username='other', password='password123')
        with self.assertLogs('basic.views.tasks', level='WARNING') as cm:
            response = self.client.get(reverse('take_task', args=[self.task.id]))
            self.assertIn("attempted to take disputed task", cm.output[0])

        self.assertRedirects(response, reverse('my_tasks'))
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Cannot modify task while a dispute is open." in m.message for m in messages))


