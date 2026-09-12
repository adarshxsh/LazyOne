from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, DisputeEvidence, DisputeVote, RewardLedger, UserProfile, Notification


class DisputeStateMachineTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        self.voter3 = User.objects.create_user(username='voter3', password='password123')

        # Give poster and taker user profiles with rewards
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        # Create task posted by poster and taken by taker
        self.task = Task.objects.create(
            title="Test Task",
            description="Task description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        # Deduct reserved reward from poster profile as in task creation
        self.poster_profile.rewards -= 200
        self.poster_profile.save()

        self.client = Client()

    def test_dispute_creation_initial_state(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair rejection"
        )
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.task.title, "Test Task")

    def test_valid_full_state_transition_lifecycle(self):
        # 1. Open -> Evidence Submission
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Work completed but unpaid")
        dispute.submit_evidence(self.taker, "Screenshot of completed work", "https://example.com/proof.png")
        self.assertEqual(dispute.status, 'evidence_submission')
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 1)

        # Poster submits evidence as well during evidence submission phase
        dispute.submit_evidence(self.poster, "Work was incomplete", "https://example.com/logs.txt")
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 2)

        # 2. Evidence Submission -> Voting
        dispute.start_voting()
        self.assertEqual(dispute.status, 'voting')

        # 3. Cast Votes
        dispute.cast_vote(self.voter1, self.taker)
        dispute.cast_vote(self.voter2, self.taker)
        dispute.cast_vote(self.voter3, self.poster)
        self.assertEqual(dispute.votes.count(), 3)

        # 4. Voting -> Resolved
        dispute.finalize_resolution()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.resolution_outcome, 'resolved_taker_wins')

        # Verify reward transfer to taker (500 + 200 = 700)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_resolution').exists())

    def test_invalid_state_transitions_raise_validation_error(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason")

        # Cannot resolve directly from 'open'
        with self.assertRaises(ValidationError):
            dispute.finalize_resolution()

        # Cannot start voting directly from 'open'
        with self.assertRaises(ValidationError):
            dispute.start_voting()

        # Direct mutation save from 'open' to 'resolved'
        dispute.status = 'resolved'
        with self.assertRaises(ValidationError):
            dispute.save()

        # Reset dispute state to open
        dispute.status = 'open'
        dispute._initial_status = 'open'

        # Submit evidence to move to evidence_submission
        dispute.submit_evidence(self.taker, "Some proof")
        self.assertEqual(dispute.status, 'evidence_submission')

        # Cannot resolve directly from evidence_submission without starting voting
        with self.assertRaises(ValidationError):
            dispute.finalize_resolution()

    def test_evidence_submission_restrictions(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason")

        # Non-participant user cannot submit evidence
        with self.assertRaises(ValidationError):
            dispute.submit_evidence(self.voter1, "Fake evidence")

        # Move to voting phase
        dispute.submit_evidence(self.taker, "Taker proof")
        dispute.start_voting()

        # Cannot submit evidence during voting phase
        with self.assertRaises(ValidationError):
            dispute.submit_evidence(self.taker, "Late evidence")

    def test_voting_phase_restrictions(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason")

        # Cannot vote during 'open' phase
        with self.assertRaises(ValidationError):
            dispute.cast_vote(self.voter1, self.taker)

        dispute.submit_evidence(self.taker, "Proof")
        dispute.start_voting()

        # Task poster and taker cannot vote on their own dispute
        with self.assertRaises(ValidationError):
            dispute.cast_vote(self.poster, self.poster)

        with self.assertRaises(ValidationError):
            dispute.cast_vote(self.taker, self.taker)

        # Single vote per neutral community member
        dispute.cast_vote(self.voter1, self.poster)
        with self.assertRaises(ValidationError):
            dispute.cast_vote(self.voter1, self.taker)

    def test_resolution_outcomes_and_reward_ledger(self):
        # 1. Poster Wins
        dispute1 = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason 1")
        dispute1.submit_evidence(self.taker, "Evidence 1")
        dispute1.start_voting()
        dispute1.cast_vote(self.voter1, self.poster)
        dispute1.finalize_resolution()

        self.assertEqual(dispute1.resolution_outcome, 'resolved_poster_wins')
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000) # 800 + 200 refunded

        # 2. Split Settlement (Tie vote)
        task2 = Task.objects.create(
            title="Task 2", description="Desc", reward=100,
            posted_by=self.poster, taken_by=self.taker, status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.poster_profile.rewards -= 100
        self.poster_profile.save()

        dispute2 = Dispute.objects.create(task=task2, raised_by=self.taker, reason="Reason 2")
        dispute2.submit_evidence(self.taker, "Evidence 2")
        dispute2.start_voting()
        dispute2.cast_vote(self.voter1, self.poster)
        dispute2.cast_vote(self.voter2, self.taker)
        dispute2.finalize_resolution()

        self.assertEqual(dispute2.resolution_outcome, 'split_settlement')
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 950) # 900 + 50
        self.assertEqual(self.taker_profile.rewards, 550) # 500 + 50

    def test_withdraw_dispute_non_destructive(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Misunderstanding")
        self.task.status = 'disputed'
        self.task.save()

        # Non-raising party cannot withdraw
        with self.assertRaises(ValidationError):
            dispute.withdraw(by_user=self.poster)

        # Raising party withdraws dispute
        dispute.withdraw(by_user=self.taker)

        # Verify dispute is still in database with 'withdrawn' status
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')

        # Verify task status is reverted to 'in_progress'
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Cannot withdraw again
        with self.assertRaises(ValidationError):
            dispute.withdraw(by_user=self.taker)

    def test_dispute_views_http_flow(self):
        self.client.login(username='taker', password='password123')
        
        # 1. Raise dispute via HTTP POST
        response = self.client.post(f'/task/dispute/{self.task.id}/', {'reason': 'View test dispute'})
        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, f'/dispute/{dispute.id}/')

        # 2. Submit evidence via HTTP POST
        response = self.client.post(f'/dispute/{dispute.id}/evidence/', {
            'evidence_text': 'HTTP evidence test',
            'evidence_url': 'https://example.com'
        })
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'evidence_submission')

        # 3. Advance to voting phase
        response = self.client.post(f'/dispute/{dispute.id}/start_voting/')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting')

        # 4. Neutral voter logs in and votes
        self.client.login(username='voter1', password='password123')
        response = self.client.post(f'/dispute/{dispute.id}/vote/', {'voted_for': self.poster.id})
        self.assertEqual(dispute.votes.count(), 1)

        # 5. Finalize resolution via HTTP POST
        response = self.client.post(f'/dispute/{dispute.id}/finalize/')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.resolution_outcome, 'resolved_poster_wins')
