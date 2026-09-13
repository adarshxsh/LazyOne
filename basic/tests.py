from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, UserProfile, Conversation
from django.utils import timezone
from datetime import timedelta

class TaskDisputeValidationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password')
        self.taker1 = User.objects.create_user(username='taker1', password='password')
        self.taker2 = User.objects.create_user(username='taker2', password='password')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker1_profile, _ = UserProfile.objects.get_or_create(user=self.taker1, defaults={'rewards': 1000})
        self.taker2_profile, _ = UserProfile.objects.get_or_create(user=self.taker2, defaults={'rewards': 1000})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password')

        self.client_taker1 = Client()
        self.client_taker1.login(username='taker1', password='password')

        self.client_taker2 = Client()
        self.client_taker2.login(username='taker2', password='password')

    def _create_task(self, **kwargs):
        task = Task.objects.create(**kwargs)
        if task.taken_by:
            conv, _ = Conversation.objects.get_or_create(task=task)
            conv.participants.add(task.posted_by, task.taken_by)
        return task

    def test_accept_cancellation_rejects_if_not_in_progress(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='disputed',
            cancellation_requested=True,
            deadline=timezone.now() + timedelta(days=1)
        )

        response = self.client_taker1.get(reverse('accept_cancellation', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertEqual(task.taken_by, self.taker1)

    def test_accept_cancellation_deletes_dispute_and_resets_available(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            cancellation_requested=True,
            deadline=timezone.now() + timedelta(days=1)
        )
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue")

        response = self.client_taker1.get(reverse('accept_cancellation', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(task=task).exists())

    def test_abandon_task_deletes_dispute_and_resets_available(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue")

        response = self.client_taker1.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(Dispute.objects.filter(task=task).exists())

    def test_withdraw_dispute_rejects_if_not_open_or_not_disputed(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='completed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue", status='resolved')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

    def test_withdraw_dispute_succeeds_when_open_and_disputed(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue", status='open')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_new_taker_can_raise_dispute_on_reopened_task(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        # Taker 1 abandons task with dispute
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Old issue")
        self.client_taker1.get(reverse('abandon_task', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')

        # Taker 2 takes task
        self.client_taker2.get(reverse('take_task', args=[task.id]))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker2)

        # Taker 2 raises dispute
        response = self.client_taker2.post(
            reverse('raise_dispute', args=[task.id]),
            {'reason': 'New issue for taker 2'}
        )
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=task, raised_by=self.taker2).exists())
        self.assertEqual(task.dispute.reason, 'New issue for taker 2')
