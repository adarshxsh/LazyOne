from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeAppeal, RewardLedger, Notification


class StaffDisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='admin', password='password123', is_staff=True)

        # UserProfiles created or ensured
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 500})

        # Set up a task and raise dispute
        self.task = Task.objects.create(
            title="Clean standard dorm",
            description="Clean room 101",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Poster did not approve task completion."
        )

        self.client = Client()

    def test_staff_arbitration_poster_wins(self):
        self.client.login(username='admin', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'poster', 'note': 'Task was incomplete'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Check atomic updates
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.poster)
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'resolved')

        # Check points refund
        self.assertEqual(self.poster_profile.rewards, 1100)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

        # Check Notifications generated for both participants
        notifs = Notification.objects.filter(link=reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(notifs.count(), 2)

    def test_staff_arbitration_taker_wins(self):
        self.client.login(username='admin', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'taker', 'note': 'Work verified'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'resolved')
        self.assertEqual(self.taker_profile.rewards, 600)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

    def test_non_staff_cannot_arbitrate(self):
        self.client.login(username='taker', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'taker', 'note': 'Self win'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertIsNone(self.dispute.winner)

    def test_single_tier_appeal_submission(self):
        # First, arbitrate as staff
        self.client.login(username='admin', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster', 'note': 'Initial ruling'})

        # Now taker appeals
        self.client.login(username='taker', password='password123')
        appeal_url = reverse('submit_appeal', args=[self.dispute.id])
        response = self.client.post(appeal_url, {'reason': 'Photo evidence shows room cleaned thoroughly.'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertTrue(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).exists())

        # Attempt duplicate appeal submission by same user
        response_dup = self.client.post(appeal_url, {'reason': 'Second appeal attempt'})
        self.assertEqual(response_dup.status_code, 302)
        self.assertEqual(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).count(), 1)

    def test_appeal_window_expired(self):
        # Arbitrate dispute
        self.client.login(username='admin', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster'})

        # Refresh from DB and set resolved_at to 3 days ago
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.dispute.resolved_at = timezone.now() - timedelta(days=3)
        self.dispute.save()

        # Taker attempts to appeal after window
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {'reason': 'Late appeal'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertFalse(DisputeAppeal.objects.filter(dispute=self.dispute).exists())

    def test_secondary_staff_review_uphold(self):
        # Arbitrate and submit appeal
        self.dispute.status = 'resolved'
        self.dispute.winner = self.poster
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        DisputeAppeal.objects.create(dispute=self.dispute, appellant=self.taker, reason="Needs review")
        self.dispute.status = 'appealed'
        self.dispute.save()

        # Staff reviews appeal and upholds
        self.client.login(username='admin', password='password123')
        review_url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(review_url, {'verdict': 'uphold', 'note': 'Initial ruling confirmed'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'finalized')
        self.assertEqual(self.dispute.final_verdict, 'uphold')
        self.assertEqual(self.dispute.winner, self.poster)

    def test_secondary_staff_review_reverse(self):
        # Arbitrate giving points to poster (+100 to poster)
        self.poster_profile.rewards += 100
        self.poster_profile.save()

        self.dispute.status = 'resolved'
        self.dispute.winner = self.poster
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        DisputeAppeal.objects.create(dispute=self.dispute, appellant=self.taker, reason="Reversal evidence")
        self.dispute.status = 'appealed'
        self.dispute.save()

        initial_poster_rewards = self.poster_profile.rewards
        initial_taker_rewards = self.taker_profile.rewards

        # Staff reviews appeal and reverses decision
        self.client.login(username='admin', password='password123')
        review_url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(review_url, {'verdict': 'reverse', 'note': 'Reversing decision based on appeal'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'finalized')
        self.assertEqual(self.dispute.final_verdict, 'reverse')
        self.assertEqual(self.dispute.winner, self.taker)

        # Points transferred from poster to taker
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards - 100)
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 100)

        # Verify dispute is finalized and locked from further appeal/arbitration
        resp_re_arbitrate = self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster'})
        self.assertEqual(resp_re_arbitrate.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'finalized')

    def test_staff_disputes_dashboard_access(self):
        self.client.login(username='admin', password='password123')
        response = self.client.get(reverse('staff_disputes_list'))
        self.assertEqual(response.status_code, 200)

        self.client.login(username='poster', password='password123')
        response_non_staff = self.client.get(reverse('staff_disputes_list'))
        self.assertEqual(response_non_staff.status_code, 302)
