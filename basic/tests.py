from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger

class DisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster, taker, non-party user, and staff user
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        # Ensure user profiles exist
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 500})

        # Create a task posted by poster, taken by taker
        # Reward reserved from poster: 200 points
        self.poster_profile.rewards -= 200
        self.poster_profile.save()
        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-200,
            transaction_type='task_creation',
            description="Reserved for task: 'Test Task'"
        )

        # Create open dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Poster did not verify task completion',
            status='open'
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_admin_dispute_queue_access(self):
        # Non-staff should be forbidden (HTTP 403)
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('admin_dispute_queue'))
        self.assertEqual(response.status_code, 403)

        # Staff should succeed (HTTP 200)
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('admin_dispute_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Task')

    def test_staff_arbitration_favor_poster(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Task was incomplete'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_decision, 'favor_poster')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'cancelled')

        # Poster gets 200 refunded (800 + 200 = 1000)
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertEqual(self.taker_profile.rewards, 500)

        # RewardLedger check
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

    def test_staff_arbitration_favor_taker(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_taker',
            'resolution_notes': 'Work was completed'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_decision, 'favor_taker')
        self.assertEqual(self.task.status, 'completed')

        # Taker gets 200 awarded (500 + 200 = 700)
        self.assertEqual(self.poster_profile.rewards, 800)
        self.assertEqual(self.taker_profile.rewards, 700)

        # RewardLedger check
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

    def test_staff_arbitration_split_50_50(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'split_50_50',
            'resolution_notes': 'Partial completion'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_decision, 'split_50_50')

        # 200 split 50/50 -> 100 to poster, 100 to taker
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.taker_profile.rewards, 600)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_resolution').first()
        taker_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_resolution').first()
        self.assertEqual(poster_ledger.amount, 100)
        self.assertEqual(taker_ledger.amount, 100)

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Self resolution attempt'
        })
        self.assertEqual(response.status_code, 403)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_submit_appeal_within_7_days(self):
        # Resolve dispute first
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })

        # Taker appeals ruling within 7 days
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'I have additional evidence of completed work.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'appealed')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'I have additional evidence of completed work.')

    def test_submit_appeal_after_7_days_rejected(self):
        # Resolve dispute and set resolved_at to 8 days ago
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })
        self.dispute.refresh_from_db()
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        # Taker tries to appeal past window
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'Late appeal submission'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'none')

    def test_single_appeal_limit(self):
        # Resolve and appeal once
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'First appeal'
        })

        # Try submitting a second appeal
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'Second appeal attempt'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_reason, 'First appeal')

    def test_non_party_cannot_submit_appeal(self):
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })

        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'Unauthorized appeal'
        })
        self.assertEqual(response.status_code, 403)

    def test_senior_staff_resolve_appeal_uphold(self):
        # Resolve favor poster, then taker appeals
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'Please reconsider'
        })

        # Senior staff upholds ruling
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_appeal', args=[self.dispute.id]), {
            'action': 'uphold',
            'appeal_notes': 'Appeal reviewed; initial ruling stand.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'appeal_rejected')
        self.assertEqual(self.dispute.resolution_decision, 'favor_poster')

    def test_senior_staff_resolve_appeal_reverse(self):
        # Initial ruling: favor_poster (Poster got 200 points refund, Taker got 0)
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'resolution_decision': 'favor_poster',
            'resolution_notes': 'Initial ruling'
        })

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertEqual(self.taker_profile.rewards, 500)

        # Taker appeals
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'appeal_reason': 'Evidence attached'
        })

        # Senior staff reverses decision to favor_taker
        self.client.login(username='staff', password='password123')
        response = self.client.post(reverse('resolve_appeal', args=[self.dispute.id]), {
            'action': 'reverse',
            'resolution_decision': 'favor_taker',
            'appeal_notes': 'Appeal granted based on video proof.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.resolution_decision, 'favor_taker')
        self.assertEqual(self.dispute.appeal_status, 'appeal_upheld')
        self.assertEqual(self.task.status, 'completed')

        # Financial integrity check:
        # Poster balance refunded initially +200, reversed -200 => 800
        # Taker balance awarded +200 => 700
        self.assertEqual(self.poster_profile.rewards, 800)
        self.assertEqual(self.taker_profile.rewards, 700)

        # RewardLedger check for appeal adjustment
        poster_adj = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_adjustment').first()
        taker_adj = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_adjustment').first()

        self.assertIsNotNone(poster_adj)
        self.assertEqual(poster_adj.amount, -200)

        self.assertIsNotNone(taker_adj)
        self.assertEqual(taker_adj.amount, 200)
