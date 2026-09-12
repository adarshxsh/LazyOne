from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, RewardLedger, Notification, UserProfile, Conversation

class DisputeAdjudicationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.random_user = User.objects.create_user(username='random', password='password123')

        # Create profiles with initial points
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1500})
        self.random_profile, _ = UserProfile.objects.get_or_create(user=self.random_user, defaults={'rewards': 1500})

        # Create a task posted by poster and taken by taker
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=2),
            status='disputed'
        )

        # Create Conversation for task to avoid NoReverseMatch in home template rendering
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create an open dispute for the task raised by taker
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Incomplete work claim",
            status='open'
        )

    def test_non_staff_cannot_adjudicate_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'payout_taker'})
        self.assertEqual(response.status_code, 403)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_adjudication_payout_taker(self):
        self.client.login(username='staff', password='password123')
        initial_taker_rewards = self.taker_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'payout_taker'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_ruling, 'payout_taker')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_adjudication_refund_poster(self):
        self.client.login(username='staff', password='password123')
        initial_poster_rewards = self.poster_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'refund_poster'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_ruling, 'refund_poster')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 200)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)

    def test_staff_adjudication_split(self):
        self.client.login(username='staff', password='password123')
        initial_poster_rewards = self.poster_profile.rewards
        initial_taker_rewards = self.taker_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {
            'ruling': 'split',
            'poster_amount': 120,
            'taker_amount': 80
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 120)
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 80)

    def test_appeal_filing_by_taker(self):
        # First resolve the dispute
        self.dispute.status = 'resolved'
        self.dispute.resolution_ruling = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.assertTrue(self.dispute.is_appealable)

        # Login as taker and submit appeal
        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'New evidence shows work was submitted on time.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'pending')
        self.assertEqual(self.dispute.appeal_filed_by, self.taker)
        self.assertEqual(self.dispute.status, 'under_appeal')
        self.assertEqual(self.dispute.appeal_reason, 'New evidence shows work was submitted on time.')

        # Verify staff notification
        self.assertTrue(Notification.objects.filter(recipient=self.staff_user).exists())

    def test_appeal_filing_unauthorized_user_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.client.login(username='random', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Random appeal'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.appeal_status, 'pending')

    def test_appeal_filing_outside_time_window_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        self.assertFalse(self.dispute.is_appealable)

        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Late appeal'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.appeal_status, 'pending')

    def test_duplicate_appeal_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Second appeal attempt'
        }, follow=True)

        # Status shouldn't change
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')

    def test_staff_review_appeal_confirm(self):
        self.dispute.status = 'under_appeal'
        self.dispute.appeal_status = 'pending'
        self.dispute.appeal_reason = 'Testing review'
        self.dispute.save()

        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/review/', {
            'action': 'reject',
            'notes': 'Original decision confirmed.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'rejected')
        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.dispute.appeal_resolution_notes, 'Original decision confirmed.')

    def test_staff_review_appeal_overturn(self):
        # Set initial resolution: refund_poster
        self.dispute.resolution_ruling = 'refund_poster'
        self.dispute.status = 'under_appeal'
        self.dispute.appeal_status = 'pending'
        self.dispute.appeal_reason = 'Valid appeal'
        self.dispute.save()

        # Poster had received refund points
        self.poster_profile.rewards = 1700
        self.poster_profile.save()

        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/review/', {
            'action': 'approve',
            'corrective_action': 'payout_taker',
            'notes': 'Overturned in favor of taker.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'approved')
        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.poster_profile.rewards, 1500)
        self.assertEqual(self.taker_profile.rewards, 1700)
