from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Notification

class DisputeResolutionTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.doer = User.objects.create_user(username='doer', password='password123')
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=1000)

        self.task = Task.objects.create(
            title='Test Escrow Task',
            description='Test description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Partial work done but disagreement on delivery',
            status='open'
        )

        self.client = Client()

    def test_staff_split_dispute_resolution_success(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 70,
            'poster_refund': 30,
            'resolution_notes': 'Doer completed 70% of subtasks.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.doer_payout, 70)
        self.assertEqual(self.dispute.poster_refund, 30)
        self.assertEqual(self.dispute.resolution_notes, 'Doer completed 70% of subtasks.')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.doer_profile.rewards, 570)
        self.assertEqual(self.poster_profile.rewards, 1030)

        doer_ledger = RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').first()
        self.assertIsNotNone(doer_ledger)
        self.assertEqual(doer_ledger.amount, 70)
        self.assertEqual(doer_ledger.task, self.task)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, 30)
        self.assertEqual(poster_ledger.task, self.task)

        self.assertTrue(Notification.objects.filter(recipient=self.doer).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_split_resolution_100_0_payout(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 100,
            'poster_refund': 0,
            'resolution_notes': 'Full payout to doer.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 600)
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.assertTrue(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').exists())
        self.assertFalse(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').exists())

    def test_split_resolution_0_100_refund(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 0,
            'poster_refund': 100,
            'resolution_notes': 'Full refund to poster.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 500)
        self.assertEqual(self.poster_profile.rewards, 1100)

        self.assertFalse(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').exists())

    def test_unbalanced_split_rejection(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 60,
            'poster_refund': 50,  # 60 + 50 = 110 != 100
            'resolution_notes': 'Invalid total'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.doer_profile.rewards, 500)
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertFalse(RewardLedger.objects.filter(task=self.task).exists())

    def test_negative_split_rejection(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': -10,
            'poster_refund': 110,
            'resolution_notes': 'Negative input'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_non_staff_authorization_denied(self):
        self.client.login(username='regular', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 50,
            'poster_refund': 50,
            'resolution_notes': 'Unauthorized attempt'
        })
        self.assertEqual(response.status_code, 403)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_cannot_resolve_already_resolved_dispute(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        # First resolution
        self.client.post(url, {
            'doer_payout': 50,
            'poster_refund': 50,
            'resolution_notes': 'First resolution'
        })
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Attempt second resolution
        response = self.client.post(url, {
            'doer_payout': 80,
            'poster_refund': 20,
            'resolution_notes': 'Second resolution attempt'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.assertEqual(self.dispute.doer_payout, 50)
        self.assertEqual(self.doer_profile.rewards, 550)

