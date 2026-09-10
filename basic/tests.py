from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification

class StaffDisputeArbitrationTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.superuser = User.objects.create_superuser(username='admin', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        # User profiles are created or updated
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})

        # Create task (poster reserved 100 points)
        self.poster_profile.rewards = 900
        self.poster_profile.save()
        self.task = Task.objects.create(
            title="Test Task",
            description="Task for testing disputes",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work submitted but not accepted",
            status='open'
        )

        self.client = Client()

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'taker_win', 'resolution_notes': 'Taker did full work'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_resolve_dispute_taker_win(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'taker_win', 'resolution_notes': 'Taker provided full proof.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'taker_win')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1100)  # 1000 + 100

        # Check ledger
        ledger = RewardLedger.objects.get(user=self.taker, transaction_type='arbitration_award')
        self.assertEqual(ledger.amount, 100)

        # Check notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_resolve_dispute_poster_win(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'poster_win', 'resolution_notes': 'Task incomplete.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'poster_win')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1000)  # 900 + 100 refund

        ledger = RewardLedger.objects.get(user=self.poster, transaction_type='arbitration_refund')
        self.assertEqual(ledger.amount, 100)

    def test_staff_resolve_dispute_split(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'split', 'resolution_notes': 'Half work done.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'split')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1050)  # 1000 + 50
        self.assertEqual(self.poster_profile.rewards, 950)   # 900 + 50

    def test_submit_appeal_success(self):
        # First resolve dispute
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'poster_win', 'resolution_notes': 'Poster wins initially'}
        )

        # Participant submits appeal
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'I have additional screenshot proof.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'I have additional screenshot proof.')

    def test_submit_appeal_past_7_days_fails(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Late appeal'}
        )
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.appeal_status)

    def test_single_appeal_limit(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Second appeal attempt'}
        )
        # Should stay pending by taker
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appealed_by, self.taker)

    def test_resolve_appeal_uphold(self):
        # Setup pending appeal
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.appeal_reason = 'More proof'
        self.dispute.save()

        self.client.login(username='admin', password='password123')
        response = self.client.post(
            reverse('resolve_appeal_admin', args=[self.dispute.id]),
            {'appeal_decision': 'uphold', 'appeal_notes': 'Initial decision was correct.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'upheld')
        self.assertEqual(self.dispute.appeal_resolved_by, self.superuser)

    def test_resolve_appeal_overturn_poster_win(self):
        # Initial: poster won -> poster gained 100 (balance 1000), taker 0 (balance 1000)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.appeal_reason = 'Misunderstood evidence'
        self.dispute.save()

        self.client.login(username='admin', password='password123')
        response = self.client.post(
            reverse('resolve_appeal_admin', args=[self.dispute.id]),
            {'appeal_decision': 'overturn', 'appeal_notes': 'Taker actually finished requirement.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'overturned')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.poster_profile.rewards, 900)  # 1000 - 100
        self.assertEqual(self.taker_profile.rewards, 1100)  # 1000 + 100

        # Check ledger adjustments
        poster_adj = RewardLedger.objects.get(user=self.poster, transaction_type='appeal_adjustment')
        taker_adj = RewardLedger.objects.get(user=self.taker, transaction_type='appeal_adjustment')
        self.assertEqual(poster_adj.amount, -100)
        self.assertEqual(taker_adj.amount, 100)

