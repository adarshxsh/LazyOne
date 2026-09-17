from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, UserProfile, RewardLedger, Conversation

class DisputeAndCancellationGuardTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker2 = User.objects.create_user(username='taker2', password='password123')
        
        # Ensure profiles exist and have points
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker1, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker2, defaults={'rewards': 1500})
        
        self.client = Client()

    def test_accept_cancellation_rejects_when_disputed_or_not_in_progress(self):
        # Create a task in disputed status with conversation
        task = Task.objects.create(
            title='Disputed Task',
            description='Test task',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='disputed',
            cancellation_requested=True
        )
        Conversation.objects.create(task=task)
        dispute = Dispute.objects.create(task=task, raised_by=self.taker1, reason='Issue', status='open')

        self.client.login(username='taker1', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]))
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        self.assertRedirects(response, reverse('my_tasks'))

    def test_withdraw_dispute_rejects_if_not_open_or_task_completed(self):
        task = Task.objects.create(
            title='Completed Task',
            description='Test task',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='completed'
        )
        Conversation.objects.create(task=task)
        dispute = Dispute.objects.create(task=task, raised_by=self.taker1, reason='Issue', status='resolved')

        self.client.login(username='taker1', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        
        task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        self.assertRedirects(response, reverse('my_tasks'))

    def test_raise_dispute_clears_cancellation_requested(self):
        task = Task.objects.create(
            title='In Progress Task',
            description='Test task',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            cancellation_requested=True
        )
        Conversation.objects.create(task=task)

        self.client.login(username='taker1', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Cannot finish'})

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertFalse(task.cancellation_requested)
        self.assertTrue(Dispute.objects.filter(task=task, raised_by=self.taker1).exists())

    def test_reassigned_task_taker_can_raise_dispute_after_cancellation(self):
        task = Task.objects.create(
            title='Reassigned Task',
            description='Test task',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            cancellation_requested=True
        )
        Conversation.objects.create(task=task)
        # Create an orphaned/resolved dispute
        Dispute.objects.create(task=task, raised_by=self.taker1, reason='Old issue', status='resolved')

        # Taker 1 accepts cancellation
        self.client.login(username='taker1', password='password123')
        self.client.get(reverse('accept_cancellation', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(Dispute.objects.filter(task=task).exists())

        # Taker 2 takes task and raises dispute
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.taker2)
        self.assertEqual(task.status, 'in_progress')

        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'New taker issue'})
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        dispute = Dispute.objects.get(task=task)
        self.assertEqual(dispute.raised_by, self.taker2)
        self.assertEqual(dispute.reason, 'New taker issue')
