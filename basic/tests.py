from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger

class DisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1500)

        self.regular_user = User.objects.create_user(username='regular', password='password123', is_staff=False)
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=1500)

        # Create a task posted by poster, taken by taker
        # Reserve 100 points from poster
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        self.task = Task.objects.create(
            title="Test Task",
            description="Task Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: 'Test Task'"
        )

        # Create open dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work done but poster refused to complete",
            status='open'
        )

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='regular', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Unauthorized'}
        )
        self.assertEqual(response.status_code, 403)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_resolve_poster_favored(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Poster was right.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'poster_favored')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'cancelled')

        # Poster should be refunded 100 points (1400 + 100 = 1500)
        self.assertEqual(self.poster_profile.rewards, 1500)
        self.assertEqual(self.taker_profile.rewards, 1500)

        ledger_entry = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='dispute_resolution'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_staff_resolve_taker_favored(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'taker_favored', 'resolution_notes': 'Taker completed work well.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'taker_favored')
        self.assertEqual(self.task.status, 'completed')

        self.assertEqual(self.poster_profile.rewards, 1400)
        self.assertEqual(self.taker_profile.rewards, 1600)

        ledger_entry = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_resolution'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_staff_resolve_split(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'split', 'resolution_notes': 'Fair split.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'split')

        self.assertEqual(self.poster_profile.rewards, 1450)
        self.assertEqual(self.taker_profile.rewards, 1550)

        p_ledger = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='dispute_resolution'
        ).first()
        t_ledger = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_resolution'
        ).first()

        self.assertIsNotNone(p_ledger)
        self.assertEqual(p_ledger.amount, 50)
        self.assertIsNotNone(t_ledger)
        self.assertEqual(t_ledger.amount, 50)

    def test_appeal_submission_and_window_expiration(self):
        # Resolve dispute first
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Resolved for poster.'}
        )

        # Taker appeals decision
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'I submitted full proof.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'I submitted full proof.')

        # Attempt second appeal should fail
        response2 = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Second appeal attempt.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')

    def test_appeal_submission_after_window_expires(self):
        # Resolve dispute and set resolved_at to 100 hours ago
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'poster_favored'
        self.dispute.resolved_at = timezone.now() - timedelta(hours=100)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Late appeal'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'none')

    def test_staff_review_appeal_uphold(self):
        # Resolve dispute and submit appeal
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Initial ruling'}
        )
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Contesting decision'}
        )

        # Staff upholds decision
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('review_appeal', args=[self.dispute.id]),
            {'decision': 'uphold'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'upheld')
        self.assertEqual(self.dispute.appeal_reviewed_by, self.staff_user)

        # Balances should remain poster=1500, taker=1500
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1500)
        self.assertEqual(self.taker_profile.rewards, 1500)

    def test_staff_review_appeal_reverse(self):
        # Resolve dispute poster_favored and submit appeal
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Initial ruling'}
        )
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Taker was right'}
        )

        # Staff reverses decision to taker_favored
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('review_appeal', args=[self.dispute.id]),
            {'decision': 'reverse', 'new_outcome': 'taker_favored'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'reversed')
        self.assertEqual(self.dispute.resolution_outcome, 'taker_favored')
        self.assertEqual(self.task.status, 'completed')

        # Poster initial was 1500 (refunded 100), after reversal poster -100 = 1400
        self.assertEqual(self.poster_profile.rewards, 1400)
        # Taker initial was 1500 (0 awarded), after reversal taker +100 = 1600
        self.assertEqual(self.taker_profile.rewards, 1600)

        # Check RewardLedger adjustment entries
        p_adj = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_adjustment').first()
        t_adj = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_adjustment').first()

        self.assertIsNotNone(p_adj)
        self.assertEqual(p_adj.amount, -100)
        self.assertIsNotNone(t_adj)
        self.assertEqual(t_adj.amount, 100)

    def test_non_participant_cannot_appeal(self):
        # Resolve dispute
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Initial ruling'}
        )
        # Regular user attempts to appeal
        self.client.login(username='regular', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Unauthorized appeal'}
        )
        self.assertEqual(response.status_code, 403)

    def test_non_staff_cannot_review_appeal(self):
        # Resolve dispute and submit appeal
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'resolution_outcome': 'poster_favored', 'resolution_notes': 'Initial ruling'}
        )
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'reason': 'Contesting decision'}
        )
        # Regular user attempts to review appeal
        self.client.login(username='regular', password='password123')
        response = self.client.post(
            reverse('review_appeal', args=[self.dispute.id]),
            {'decision': 'reverse'}
        )
        self.assertEqual(response.status_code, 403)

