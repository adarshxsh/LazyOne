import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from .models import UserProfile, FriendRequest, Friendship, Notification

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
