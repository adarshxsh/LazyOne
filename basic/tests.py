from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from django.urls import reverse

from basic.models import UserProfile, Task, Dispute, DisputeAppeal, RewardLedger, Notification


class DisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Create taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create non-involved user
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=500)

        # Create task posted by poster, taken by taker, in disputed state
        self.task = Task.objects.create(
            title='Fix leaky faucet',
            description='Kitchen faucet needs fixing',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=2),
            status='disputed'
        )
        # Deduct initial reward points reserved for task creation
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: 'Fix leaky faucet'"
        )

        # Create open dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Poster refused to confirm work completed',
            status='open'
        )

    def test_staff_dashboard_access_control(self):
        # Non-staff user access
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('staff_dispute_dashboard'))
        self.assertEqual(response.status_code, 302)

        # Staff user access
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('staff_dispute_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fix leaky faucet')

    def test_staff_arbitrate_dispute_refund_poster(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'resolution': 'refund_poster',
            'notes': 'Work was incomplete and unsatisfactory.'
        })

        self.assertEqual(response.status_code, 302)
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'refund_poster')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)
        self.assertEqual(self.task.status, 'cancelled')

        # Poster gets 100 points back (was 900, now 1000)
        self.assertEqual(self.poster_profile.rewards, 1000)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_arbitration').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

        # Notifications check
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_arbitrate_dispute_pay_taker(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'resolution': 'pay_taker',
            'notes': 'Work was verified complete.'
        })

        self.assertEqual(response.status_code, 302)
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'pay_taker')
        self.assertEqual(self.task.status, 'completed')

        # Taker gets 100 points (was 500, now 600)
        self.assertEqual(self.taker_profile.rewards, 600)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_arbitration').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

    def test_submit_appeal_within_window(self):
        # Resolve dispute first
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'reason': 'The plumbing was fixed according to spec. Attached photo evidence in chat.'
        })

        self.assertEqual(response.status_code, 302)
        appeal = DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).first()
        self.assertIsNotNone(appeal)
        self.assertEqual(appeal.status, 'pending')

        # Check opponent notification
        self.assertTrue(Notification.objects.filter(recipient=self.poster, message__contains='submitted a formal appeal').exists())

    def test_submit_appeal_after_window_expired(self):
        # Resolve dispute 8 days ago
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'reason': 'Late appeal submission'
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).exists())

    def test_prevent_duplicate_appeal_per_party(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        # Submit first appeal
        DisputeAppeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            reason='First appeal reason'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'reason': 'Second appeal attempt'
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).count(), 1)

    def test_review_appeal_uphold(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        appeal = DisputeAppeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            reason='Taker appeals refund'
        )

        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('review_appeal', args=[appeal.id]), {
            'action': 'uphold',
            'notes': 'Initial ruling was correct based on evidence.'
        })

        self.assertEqual(response.status_code, 302)
        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'upheld')
        self.assertEqual(appeal.reviewed_by, self.staff_user)

    def test_review_appeal_overturn(self):
        # Initial ruling: refund_poster (poster had +100 refunded back to 1000, taker stays 500)
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        appeal = DisputeAppeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            reason='Taker provides conclusive proof'
        )

        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('review_appeal', args=[appeal.id]), {
            'action': 'overturn',
            'new_resolution': 'pay_taker',
            'notes': 'New evidence shows job was completed.'
        })

        self.assertEqual(response.status_code, 302)
        appeal.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(appeal.status, 'overturned')
        self.assertEqual(appeal.new_resolution, 'pay_taker')
        self.assertEqual(self.dispute.resolution_outcome, 'pay_taker')
        self.assertEqual(self.task.status, 'completed')

        # Poster's points reversed from 1000 back down to 900
        self.assertEqual(self.poster_profile.rewards, 900)

        # Taker's points awarded 100 (500 -> 600)
        self.assertEqual(self.taker_profile.rewards, 600)

        # Verify ledger entries created
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_reversal', amount=-100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_arbitration', amount=100).exists())
