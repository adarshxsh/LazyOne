import tempfile
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.urls import reverse

from basic.models import (
    UserProfile, Task, Dispute, DisputeEvidence, JuryAssignment, DisputeVote, RewardLedger, Conversation
)
from basic.services import DisputeLifecycleService


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
        self.assertIn(dispute.status, ['open', 'evidence_collection'])
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
        self.assertIn(dispute.status, ['resolved', 'cancelled'])

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

        dispute = Dispute.objects.get(task=self.task)

        # Resolve dispute in favor of taker
        DisputeLifecycleService.resolve_dispute(dispute, 'taker')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertIn(dispute.status, ['resolved', 'resolved_taker'])

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


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    MEDIA_ROOT=tempfile.mkdtemp()
)
class DisputeLifecycleEngineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user('poster', 'poster@example.com', 'password123')
        self.taker = User.objects.create_user('taker', 'taker@example.com', 'password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, rewards=1000)
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, rewards=1000)

        # Create 10 neutral users for jury pool
        self.jurors = []
        for i in range(10):
            u = User.objects.create_user(f'juror{i}', f'juror{i}@example.com', 'password123')
            UserProfile.objects.get_or_create(user=u, rewards=500)
            self.jurors.append(u)

        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

        self.client = Client()

    def test_dispute_creation_and_evidence_collection_phase(self):
        # 1. Raise dispute
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="Work completed but not accepted."
        )

        self.assertEqual(dispute.status, Dispute.EVIDENCE_COLLECTION)
        self.assertEqual(self.task.status, 'disputed')
        self.assertIsNotNone(dispute.evidence_deadline)

        # 2. Both parties submit evidence
        evidence1 = DisputeLifecycleService.submit_evidence(
            dispute=dispute,
            user=self.taker,
            text_evidence="Proof of task submission attached."
        )
        self.assertEqual(evidence1.submitted_by, self.taker)
        self.assertEqual(dispute.status, Dispute.EVIDENCE_COLLECTION)

        evidence2 = DisputeLifecycleService.submit_evidence(
            dispute=dispute,
            user=self.poster,
            text_evidence="Task was not completed as agreed."
        )
        self.assertEqual(evidence2.submitted_by, self.poster)

        # Dispute should automatically transition to JURY_SELECTION then VOTING once 5 neutral jurors are assigned
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.VOTING)
        self.assertEqual(dispute.jury_assignments.count(), 5)

        # Ensure poster and taker are excluded from jury pool
        juror_ids = set(dispute.jury_assignments.values_list('juror_id', flat=True))
        self.assertNotIn(self.poster.id, juror_ids)
        self.assertNotIn(self.taker.id, juror_ids)

    def test_friend_exclusion_in_jury_selection(self):
        # Make juror0 a friend of poster
        self.poster_profile.friends.add(UserProfile.objects.get(user=self.jurors[0]))

        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.poster,
            reason="Quality issues"
        )
        DisputeLifecycleService.submit_evidence(dispute, self.poster, text_evidence="Evidence A")
        DisputeLifecycleService.submit_evidence(dispute, self.taker, text_evidence="Evidence B")

        dispute.refresh_from_db()
        juror_ids = set(dispute.jury_assignments.values_list('juror_id', flat=True))
        self.assertNotIn(self.jurors[0].id, juror_ids)

    def test_voting_quorum_and_automated_settlement_taker_wins(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="Dispute for settlement"
        )
        DisputeLifecycleService.submit_evidence(dispute, self.taker, text_evidence="Ev1")
        DisputeLifecycleService.submit_evidence(dispute, self.poster, text_evidence="Ev2")

        dispute.refresh_from_db()
        assigned_jurors = [ja.juror for ja in dispute.jury_assignments.all()]
        self.assertEqual(len(assigned_jurors), 5)

        # 3 jurors vote for taker
        for j in assigned_jurors[:3]:
            DisputeLifecycleService.cast_vote(dispute, j, choice='taker')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.APPEAL)
        self.assertEqual(dispute.winning_side, 'taker')

        # Execute final resolution
        DisputeLifecycleService.resolve_dispute(dispute, 'taker')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, Dispute.RESOLVED_TAKER)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1200) # 1000 + 200

        ledger_entry = RewardLedger.objects.get(
            user=self.taker,
            transaction_type='dispute_settlement_taker'
        )
        self.assertEqual(ledger_entry.amount, 200)

    def test_voting_quorum_and_automated_settlement_poster_wins(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.poster,
            reason="Dispute for poster refund"
        )
        DisputeLifecycleService.submit_evidence(dispute, self.poster, text_evidence="Ev1")
        DisputeLifecycleService.submit_evidence(dispute, self.taker, text_evidence="Ev2")

        dispute.refresh_from_db()
        assigned_jurors = [ja.juror for ja in dispute.jury_assignments.all()]

        # 3 jurors vote for poster
        for j in assigned_jurors[:3]:
            DisputeLifecycleService.cast_vote(dispute, j, choice='poster')

        dispute.refresh_from_db()
        DisputeLifecycleService.resolve_dispute(dispute, 'poster')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, Dispute.RESOLVED_POSTER)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 + 200 refund

        ledger_entry = RewardLedger.objects.get(
            user=self.poster,
            transaction_type='dispute_settlement_poster'
        )
        self.assertEqual(ledger_entry.amount, 200)

    def test_dispute_withdrawal(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="Mistake raising dispute"
        )
        DisputeLifecycleService.withdraw_dispute(dispute, self.taker)

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, Dispute.CANCELLED)
        self.assertEqual(self.task.status, 'in_progress')

    def test_duplicate_vote_prevention(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="Reason"
        )
        DisputeLifecycleService.submit_evidence(dispute, self.taker, text_evidence="Ev1")
        DisputeLifecycleService.submit_evidence(dispute, self.poster, text_evidence="Ev2")

        dispute.refresh_from_db()
        juror = dispute.jury_assignments.first().juror

        DisputeLifecycleService.cast_vote(dispute, juror, choice='poster')
        with self.assertRaises(ValidationError):
            DisputeLifecycleService.cast_vote(dispute, juror, choice='taker')

    def test_dispute_detail_view_permissions_and_render(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="View test"
        )

        # Non-participant user should be denied access
        other_user = User.objects.create_user('other', 'other@example.com', 'password123')
        self.client.login(username='other', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(res, reverse('home'), fetch_redirect_response=False)

        # Taker should have access
        self.client.login(username='taker', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Dispute Details")
        self.assertContains(res, "Evidence Collection")

    def test_view_post_evidence_submission_and_voting_flow(self):
        dispute = DisputeLifecycleService.raise_dispute(
            task=self.task,
            user=self.taker,
            reason="View flow test"
        )

        # Taker submits evidence via POST
        self.client.login(username='taker', password='password123')
        res = self.client.post(reverse('submit_evidence', args=[dispute.id]), {
            'text_evidence': 'Taker evidence text',
            'external_link': 'https://example.com'
        })
        self.assertRedirects(res, reverse('dispute_detail', args=[dispute.id]))

        # Poster submits evidence via POST
        self.client.login(username='poster', password='password123')
        res = self.client.post(reverse('submit_evidence', args=[dispute.id]), {
            'text_evidence': 'Poster evidence text'
        })
        self.assertRedirects(res, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.VOTING)

        assigned_juror = dispute.jury_assignments.first().juror
        self.client.login(username=assigned_juror.username, password='password123')
        res = self.client.post(reverse('cast_vote', args=[dispute.id]), {
            'choice': 'taker'
        })
        self.assertRedirects(res, reverse('dispute_detail', args=[dispute.id]))
        self.assertTrue(dispute.votes.filter(voter=assigned_juror).exists())
