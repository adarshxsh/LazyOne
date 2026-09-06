from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from basic.models import UserProfile, Task, RewardLedger, Conversation, Notification, FriendRequest, Friendship, Dispute


class TaskTransactionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user1 = User.objects.create_user(username='user1', password='password123', email='user1@example.com')
        self.user2 = User.objects.create_user(username='user2', password='password123', email='user2@example.com')
        self.profile1, _ = UserProfile.objects.get_or_create(user=self.user1, defaults={'rewards': 1000})
        self.profile1.rewards = 1000
        self.profile1.save()
        self.profile2, _ = UserProfile.objects.get_or_create(user=self.user2, defaults={'rewards': 500})
        self.profile2.rewards = 500
        self.profile2.save()

    def test_add_task_success(self):
        self.client.login(username='user1', password='password123')
        deadline = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(reverse('add_task'), {
            'title': 'Test Task 1',
            'description': 'Description 1',
            'reward': '200',
            'deadline': deadline
        })
        self.assertEqual(response.status_code, 302)
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.rewards, 800)
        task = Task.objects.get(title='Test Task 1')
        self.assertEqual(task.reward, 200)
        self.assertEqual(task.status, 'available')
        ledger = RewardLedger.objects.get(task=task, user=self.user1)
        self.assertEqual(ledger.amount, -200)
        self.assertEqual(ledger.transaction_type, 'task_creation')

    def test_add_task_insufficient_rewards(self):
        self.client.login(username='user1', password='password123')
        deadline = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(reverse('add_task'), {
            'title': 'Expensive Task',
            'description': 'Description 1',
            'reward': '1500',
            'deadline': deadline
        })
        self.assertEqual(response.status_code, 200)
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.rewards, 1000)
        self.assertFalse(Task.objects.filter(title='Expensive Task').exists())

    def test_take_task(self):
        deadline = timezone.now() + timedelta(days=2)
        task = Task.objects.create(
            title='Available Task', description='Desc', reward=100,
            posted_by=self.user1, deadline=deadline, status='available'
        )
        self.client.login(username='user2', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.user2)
        self.assertTrue(Conversation.objects.filter(task=task).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user1).exists())

    def test_complete_task(self):
        deadline = timezone.now() + timedelta(days=2)
        task = Task.objects.create(
            title='Task in progress', description='Desc', reward=150,
            posted_by=self.user1, taken_by=self.user2, deadline=deadline, status='in_progress'
        )
        self.client.login(username='user1', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.profile2.refresh_from_db()
        self.assertEqual(self.profile2.rewards, 650)
        ledger = RewardLedger.objects.get(task=task, user=self.user2)
        self.assertEqual(ledger.amount, 150)
        self.assertEqual(ledger.transaction_type, 'task_completion')

    def test_cancel_task(self):
        deadline = timezone.now() + timedelta(days=2)
        task = Task.objects.create(
            title='Task to cancel', description='Desc', reward=200,
            posted_by=self.user1, deadline=deadline, status='available'
        )
        self.client.login(username='user1', password='password123')
        response = self.client.get(reverse('cancel_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.rewards, 1200)

    def test_accept_cancellation(self):
        deadline = timezone.now() + timedelta(days=2)
        task = Task.objects.create(
            title='Task cancellation requested', description='Desc', reward=300,
            posted_by=self.user1, taken_by=self.user2, deadline=deadline,
            status='in_progress', cancellation_requested=True
        )
        self.client.login(username='user2', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]))
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(task.cancellation_requested)
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.rewards, 1300)


class FriendTransactionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user1 = User.objects.create_user(username='fuser1', password='password123')
        self.user2 = User.objects.create_user(username='fuser2', password='password123')
        self.profile1, _ = UserProfile.objects.get_or_create(user=self.user1)
        self.profile2, _ = UserProfile.objects.get_or_create(user=self.user2)

    def test_friend_request_flow(self):
        self.client.login(username='fuser1', password='password123')
        response = self.client.post(reverse('send_friend_request', args=[self.user2.id]), {'closeness': 80})
        self.assertEqual(response.status_code, 302)

        freq = FriendRequest.objects.get(from_user=self.user1, to_user=self.user2)

        self.client.login(username='fuser2', password='password123')
        response = self.client.get(reverse('accept_friend_request', args=[freq.id]))
        self.assertEqual(response.status_code, 302)

        self.profile1.refresh_from_db()
        self.profile2.refresh_from_db()
        self.assertTrue(self.profile1.friends.filter(id=self.profile2.id).exists())
        self.assertTrue(self.profile2.friends.filter(id=self.profile1.id).exists())
        self.assertTrue(Friendship.objects.filter(from_user=self.profile1, to_user=self.profile2).exists())


class DisputeTransactionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user1 = User.objects.create_user(username='duser1', password='password123')
        self.user2 = User.objects.create_user(username='duser2', password='password123')
        self.profile1, _ = UserProfile.objects.get_or_create(user=self.user1)
        self.profile2, _ = UserProfile.objects.get_or_create(user=self.user2)
        deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title='Disputed Task', description='Desc', reward=100,
            posted_by=self.user1, taken_by=self.user2, deadline=deadline, status='in_progress'
        )

    def test_raise_and_withdraw_dispute(self):
        self.client.login(username='duser2', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work not clear'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.reason, 'Work not clear')

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())


class NetworkRequestBoundaryTests(TestCase):
    @patch('basic.views.authentication.send_mail')
    def test_register_send_mail_outside_tx(self, mock_send_mail):
        mock_send_mail.return_value = 1
        client = Client()
        response = client.post(reverse('register'), {
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password1': 'Password123!',
            'password2': 'Password123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username='newuser').exists())
        mock_send_mail.assert_called_once()
