from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification, FriendRequest, Friendship


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Create task: reward = 300, 20% = 60 (> 50 minimum)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create small reward task: reward = 100, 20% = 20 (min 50 applies)
        self.small_task = Task.objects.create(
            title="Small Task",
            description="Small Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.small_task)

    def test_deposit_bond_calculation(self):
        # 20% of 300 = 60 (> 50)
        self.assertEqual(self.task.deposit_bond_amount, 60)
        # 20% of 100 = 20 (< 50, so minimum 50 applies)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
        # Set taker rewards to 30 (less than 60 required)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balance should remain unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        # First raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 40 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


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
        self.profile2, _ = UserProfile.objects.get_or_create(user=self.user2, defaults={'rewards': 100})
        self.profile2.rewards = 100
        self.profile2.save()
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
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')


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

