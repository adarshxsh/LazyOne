import json
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User
from basic.models import UserProfile, FriendRequest, Friendship
from unittest.mock import patch, MagicMock

class SafeOfflineFallbackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser@example.com', password='password')
        # Ensure UserProfile is created
        self.profile, created = UserProfile.objects.get_or_create(user=self.user)
        self.client.login(username='testuser@example.com', password='password')

    def test_verify_phone_route_exists(self):
        # Verify the route resolves and is accessible
        response = self.client.get(reverse('verify_phone_token'))
        # Should return Method Not Allowed (405) since it is a GET request
        self.assertEqual(response.status_code, 405)

    def test_verify_phone_token_mock_success(self):
        # Verify simulated/mock verification with "mock_+15550222"
        payload = {'token': 'mock_+15550222'}
        response = self.client.post(
            reverse('verify_phone_token'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Verify local model state is updated correctly
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_phone_verified)
        self.assertEqual(self.profile.phone_number, '+15550222')

    def test_verify_phone_token_offline_token_success(self):
        # Verify simulated/mock verification with "offline_token"
        payload = {'token': 'offline_token'}
        response = self.client.post(
            reverse('verify_phone_token'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_phone_verified)
        self.assertEqual(self.profile.phone_number, '+15550100')

    @patch('firebase_admin.auth.verify_id_token')
    def test_verify_phone_token_exception_fallback(self, mock_verify):
        # Mock auth.verify_id_token to raise an Exception (simulating network failure/offline)
        mock_verify.side_effect = Exception("Network timeout")
        
        payload = {'token': 'some_real_looking_token'}
        response = self.client.post(
            reverse('verify_phone_token'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        # Should NOT raise 500. Should fallback gracefully and return 200 success with simulated phone
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_phone_verified)
        self.assertEqual(self.profile.phone_number, '+15550100')

    def test_user_list_offline_safeguard(self):
        # Verify user_list renders correctly without any Firestore service active
        other_user = User.objects.create_user(username='otheruser@example.com', password='password')
        other_profile, created = UserProfile.objects.get_or_create(user=other_user)
        other_profile.first_name = "Jane"
        other_profile.last_name = "Doe"
        other_profile.save()

        response = self.client.get(reverse('user_list'))
        self.assertEqual(response.status_code, 200)
        
        # Check context
        self.assertIn('local_users_json', response.context)
        local_users = json.loads(response.context['local_users_json'])
        self.assertEqual(len(local_users), 1)
        self.assertEqual(local_users[0]['username'], 'otheruser@example.com')
        self.assertEqual(local_users[0]['name'], 'Jane Doe')
