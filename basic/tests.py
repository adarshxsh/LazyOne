import json
from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from .models import (
    UserProfile, Task, Dispute, RewardLedger, Conversation,
    FriendRequest, Friendship, Notification
)


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


class UserProfileAndSocialTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username='alice', email='alice@example.com', password='password123')
        self.user2 = User.objects.create_user(username='bob', email='bob@example.com', password='password123')
        self.user3 = User.objects.create_user(username='charlie', email='charlie@example.com', password='password123')

        self.profile1, _ = UserProfile.objects.get_or_create(user=self.user1, defaults={'firebase_uid': 'uid_alice', 'closeness': 80})
        self.profile2, _ = UserProfile.objects.get_or_create(user=self.user2, defaults={'firebase_uid': 'uid_bob', 'closeness': 60})
        self.profile3, _ = UserProfile.objects.get_or_create(user=self.user3, defaults={'firebase_uid': 'uid_charlie', 'closeness': 50})

        self.client1 = Client()
        self.client1.login(username='alice', password='password123')

        self.client2 = Client()
        self.client2.login(username='bob', password='password123')

    def test_user_profile_schema_fields(self):
        self.assertEqual(self.profile1.firebase_uid, 'uid_alice')
        self.assertEqual(self.profile1.closeness, 80)
        self.assertEqual(self.profile2.firebase_uid, 'uid_bob')
        self.assertEqual(self.profile2.closeness, 60)

    def test_friend_request_closeness_field(self):
        freq = FriendRequest.objects.create(from_user=self.user1, to_user=self.user2, closeness=75)
        self.assertEqual(freq.closeness, 75)

    def test_user_list_view(self):
        response = self.client1.get(reverse('user_list'))
        self.assertEqual(response.status_code, 200)
        exclude_uids = json.loads(response.context['exclude_uids_json'])
        self.assertIn('uid_alice', exclude_uids)

    def test_send_friend_request(self):
        url = reverse('send_friend_request', kwargs={'user_id': self.user2.id})
        response = self.client1.post(url, {'closeness': 70})
        self.assertRedirects(response, reverse('friends'))

        freq = FriendRequest.objects.filter(from_user=self.user1, to_user=self.user2).first()
        self.assertIsNotNone(freq)
        self.assertEqual(freq.closeness, 70)

        # Notification created
        notif = Notification.objects.filter(recipient=self.user2).first()
        self.assertIsNotNone(notif)
        self.assertIn('alice sent you a friend request', notif.message)

    def test_send_friend_request_to_self(self):
        url = reverse('send_friend_request', kwargs={'user_id': self.user1.id})
        response = self.client1.post(url, {'closeness': 50})
        self.assertRedirects(response, reverse('friends'))

        freq = FriendRequest.objects.filter(from_user=self.user1, to_user=self.user1).first()
        self.assertIsNone(freq)

    def test_accept_friend_request(self):
        freq = FriendRequest.objects.create(from_user=self.user1, to_user=self.user2, closeness=85)
        url = reverse('accept_friend_request', kwargs={'request_id': freq.id})
        response = self.client2.post(url)
        self.assertRedirects(response, reverse('friends'))

        # Check Friendship created bidirectional
        f1 = Friendship.objects.filter(from_user=self.profile1, to_user=self.profile2).first()
        f2 = Friendship.objects.filter(from_user=self.profile2, to_user=self.profile1).first()

        self.assertIsNotNone(f1)
        self.assertIsNotNone(f2)
        self.assertEqual(f1.closeness, 85)
        self.assertEqual(f2.closeness, 85)

        # Check request deleted
        self.assertFalse(FriendRequest.objects.filter(id=freq.id).exists())

        # Check friends M2M
        self.assertTrue(self.profile1.friends.filter(id=self.profile2.id).exists())
        self.assertTrue(self.profile2.friends.filter(id=self.profile1.id).exists())

    def test_decline_friend_request(self):
        freq = FriendRequest.objects.create(from_user=self.user1, to_user=self.user2, closeness=50)
        url = reverse('decline_friend_request', kwargs={'request_id': freq.id})
        response = self.client2.post(url)
        self.assertRedirects(response, reverse('friends'))

        self.assertFalse(FriendRequest.objects.filter(id=freq.id).exists())

    def test_update_closeness(self):
        f1 = Friendship.objects.create(from_user=self.profile1, to_user=self.profile2, closeness=50)
        f2 = Friendship.objects.create(from_user=self.profile2, to_user=self.profile1, closeness=50)

        url = reverse('update_closeness', kwargs={'friendship_id': f1.id})
        response = self.client1.post(url, {'closeness': 90})
        self.assertRedirects(response, reverse('user_profile', kwargs={'user_id': self.user2.id}))

        f1.refresh_from_db()
        f2.refresh_from_db()
        self.assertEqual(f1.closeness, 90)
        self.assertEqual(f2.closeness, 90)

    def test_user_profile_view_friendship_status(self):
        # Create friendship
        self.profile1.friends.add(self.profile2)
        self.profile2.friends.add(self.profile1)
        f1 = Friendship.objects.create(from_user=self.profile1, to_user=self.profile2, closeness=65)

        url = reverse('user_profile', kwargs={'user_id': self.user2.id})
        response = self.client1.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_friend'])
        self.assertEqual(response.context['friendship'].closeness, 65)
