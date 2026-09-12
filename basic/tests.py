from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, UserProfile, Conversation

class DisputeAndTaskStatusCheckTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker2 = User.objects.create_user(username='taker2', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.taker2, rewards=1000)

    def _create_task_with_conversation(self, **kwargs):
        task = Task.objects.create(**kwargs)
        if task.taken_by:
            conv = Conversation.objects.create(task=task)
            conv.participants.add(task.posted_by, task.taken_by)
        return task

    def test_accept_cancellation_success_and_cleans_up_dispute(self):
        task = self._create_task_with_conversation(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            cancellation_requested=True
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='open'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_accept_cancellation_rejects_non_in_progress_tasks(self):
        self.client.login(username='taker', password='password123')

        for invalid_status in ['disputed', 'completed', 'cancelled']:
            task = self._create_task_with_conversation(
                title=f'Test Task {invalid_status}',
                description='Test Description',
                reward=100,
                posted_by=self.poster,
                taken_by=self.taker,
                status=invalid_status,
                cancellation_requested=True
            )

            response = self.client.get(reverse('accept_cancellation', args=[task.id]))
            self.assertEqual(response.status_code, 404)
            task.refresh_from_db()
            self.assertEqual(task.status, invalid_status)

    def test_withdraw_dispute_success(self):
        task = self._create_task_with_conversation(
            title='Disputed Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='open'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_fails_if_dispute_not_open(self):
        task = self._create_task_with_conversation(
            title='Disputed Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertEqual(response.status_code, 404)

    def test_withdraw_dispute_cannot_modify_completed_or_cancelled_tasks(self):
        self.client.login(username='taker', password='password123')

        for task_status in ['completed', 'cancelled']:
            task = self._create_task_with_conversation(
                title=f'Task {task_status}',
                description='Test Description',
                reward=100,
                posted_by=self.poster,
                taken_by=self.taker,
                status=task_status
            )
            dispute = Dispute.objects.create(
                task=task,
                raised_by=self.taker,
                reason='Test reason',
                status='open'
            )

            response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
            self.assertEqual(response.status_code, 404)

            task.refresh_from_db()
            self.assertEqual(task.status, task_status)
            self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_raise_dispute_on_reopened_task(self):
        task = self._create_task_with_conversation(
            title='Reopened Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            cancellation_requested=True
        )
        old_dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Old dispute',
            status='open'
        )

        # Taker accepts cancellation which resets task to available and deletes dispute
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertFalse(Dispute.objects.filter(id=old_dispute.id).exists())

        # Second taker takes the task
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker2)

        # Second taker raises a dispute
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'New taker dispute'})
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=task, raised_by=self.taker2).exists())
        new_dispute = Dispute.objects.get(task=task)
        self.assertRedirects(response, reverse('dispute_detail', args=[new_dispute.id]))
