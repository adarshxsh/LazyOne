from django.test import TestCase, Client, TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from .models import Task, UserProfile, Dispute
from datetime import timedelta
import threading

class ArchitecturalFixesTests(TransactionTestCase): # Use TransactionTestCase for on_commit hooks and thread-based db access
    def setUp(self):
        self.client1 = Client()
        self.client2 = Client()
        self.client3 = Client()
        
        self.user1 = User.objects.create_user(username='user1', password='password')
        self.user2 = User.objects.create_user(username='user2', password='password')
        self.user3 = User.objects.create_user(username='user3', password='password')
        
        UserProfile.objects.create(user=self.user1, rewards=1000)
        UserProfile.objects.create(user=self.user2, rewards=1000)
        UserProfile.objects.create(user=self.user3, rewards=1000)
        
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.user1,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

    def test_task_assignment_race_condition(self):
        # Simulate two users trying to take the same task simultaneously
        self.client2.login(username='user2', password='password')
        self.client3.login(username='user3', password='password')

        def user2_take_task():
            self.client2.get(reverse('take_task', args=[self.task.id]))
            
        def user3_take_task():
            self.client3.get(reverse('take_task', args=[self.task.id]))

        t1 = threading.Thread(target=user2_take_task)
        t2 = threading.Thread(target=user3_take_task)
        
        t1.start()
        t2.start()
        
        t1.join()
        t2.join()
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertIsNotNone(self.task.taken_by)

    def test_reward_updates_atomic_F_expression(self):
        self.client2.login(username='user2', password='password')
        
        # User2 takes task
        self.client2.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        
        # User1 completes task (as poster)
        self.client1.login(username='user1', password='password')
        self.client1.get(reverse('complete_task', args=[self.task.id]))
        
        # Verify User2's rewards went up by 100
        user2_profile = UserProfile.objects.get(user=self.user2)
        self.assertEqual(user2_profile.rewards, 1100) # 1000 + 100

    @patch('basic.models.sync_dispute_to_firestore')
    def test_dispute_creation_triggers_on_commit(self, mock_sync):
        self.client2.login(username='user2', password='password')
        self.client2.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        
        # User2 raises a dispute
        response = self.client2.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Payment delayed'})
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.user2)
        
        # Assert the sync function was called after transaction committed
        mock_sync.assert_called_once_with(dispute.id, False)
