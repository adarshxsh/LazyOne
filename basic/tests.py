from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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
        self.assertEqual(dispute.status, 'open')
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
        self.assertEqual(dispute.status, 'resolved')

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

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

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


from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from .models import DisputeEvidence, DisputeVote

class DisputeEvidenceAndJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.neutral_user1 = User.objects.create_user(username='jury1', password='password123')
        UserProfile.objects.create(user=self.neutral_user1, rewards=100)

        self.neutral_user2 = User.objects.create_user(username='jury2', password='password123')
        UserProfile.objects.create(user=self.neutral_user2, rewards=100)

        self.neutral_user3 = User.objects.create_user(username='jury3', password='password123')
        UserProfile.objects.create(user=self.neutral_user3, rewards=100)

        # Task
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Multi-Phase Task",
            description="Task to test multi-phase dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_multi_phase_state_transitions(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work done but not accepted",
            deposit_amount=50,
            status='open'
        )

        # Transition open -> evidence_submission
        self.assertTrue(dispute.can_transition_to('evidence_submission'))
        self.assertTrue(dispute.transition_to('evidence_submission'))
        self.assertEqual(dispute.status, 'evidence_submission')

        # Transition evidence_submission -> under_review
        self.assertTrue(dispute.can_transition_to('under_review'))
        self.assertTrue(dispute.transition_to('under_review'))
        self.assertEqual(dispute.status, 'under_review')

        # Transition under_review -> voting
        self.assertTrue(dispute.can_transition_to('voting'))
        self.assertTrue(dispute.transition_to('voting'))
        self.assertEqual(dispute.status, 'voting')

        # Invalid backwards transition: voting -> open should fail
        self.assertFalse(dispute.can_transition_to('open'))
        self.assertFalse(dispute.transition_to('open'))
        self.assertEqual(dispute.status, 'voting')

    def test_evidence_submission_by_participant(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Initial dispute reason",
            deposit_amount=50,
            status='open'
        )

        # Taker submits text and file evidence
        self.client.login(username='taker', password='password123')
        file_data = SimpleUploadedFile("screenshot.jpg", b"file_content", content_type="image/jpeg")
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'text_evidence': 'Here is proof of work done', 'file_evidence': file_data}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'evidence_submission')

        evidence = DisputeEvidence.objects.filter(dispute=dispute, submitted_by=self.taker).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.text_evidence, 'Here is proof of work done')
        self.assertTrue(evidence.file_evidence.name.endswith('screenshot.jpg'))

        # Non-participant attempts to submit evidence -> rejected
        self.client.login(username='jury1', password='password123')
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'text_evidence': 'Unauthorized evidence'}
        )
        self.assertEqual(DisputeEvidence.objects.filter(submitted_by=self.neutral_user1).count(), 0)

    def test_jury_voting_eligibility_and_guardrails(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Task dispute",
            deposit_amount=50,
            status='evidence_submission'
        )

        # Attempting to vote during evidence_submission phase -> rejected
        self.client.login(username='jury1', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'comment': 'Good work'}
        )
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Advance phase to voting
        dispute.transition_to('voting')

        # Task participant (taker) attempts to vote -> rejected
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertEqual(DisputeVote.objects.filter(voter=self.taker).count(), 0)

        # Neutral user votes for taker -> success
        self.client.login(username='jury1', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'comment': 'Evidence is convincing'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(voter=self.neutral_user1).count(), 1)

        # Neutral user attempts duplicate vote -> rejected
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertEqual(DisputeVote.objects.filter(voter=self.neutral_user1).count(), 1)

    def test_dispute_resolution_via_jury_votes(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Taker completed work",
            deposit_amount=50,
            status='voting'
        )

        # 2 neutral users vote for Taker, 1 for Poster
        DisputeVote.objects.create(dispute=dispute, voter=self.neutral_user1, voted_for=self.taker, comment='Taker submitted proof')
        DisputeVote.objects.create(dispute=dispute, voter=self.neutral_user2, voted_for=self.taker, comment='Agreed with taker')
        DisputeVote.objects.create(dispute=dispute, voter=self.neutral_user3, voted_for=self.poster, comment='Favor poster')

        # Resolve dispute
        dispute.resolve_dispute()

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: 500 + 200 (task reward) + 50 (deposit refund) = 750
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 750)

    def test_sla_command_phase_transitions(self):
        # Create dispute 10 days ago (expired SLA) in 'open' phase
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Old dispute",
            deposit_amount=50,
            status='open'
        )
        Dispute.objects.filter(id=dispute.id).update(
            created_at=timezone.now() - timedelta(days=10),
            updated_at=timezone.now() - timedelta(days=10)
        )

        # Run resolve_expired_disputes with --days 7
        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        # open -> under_review
        self.assertEqual(dispute.status, 'under_review')

        # Fast-forward updated_at for under_review phase
        Dispute.objects.filter(id=dispute.id).update(
            updated_at=timezone.now() - timedelta(days=10)
        )
        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        # under_review -> voting
        self.assertEqual(dispute.status, 'voting')

        # Add a vote for poster
        DisputeVote.objects.create(dispute=dispute, voter=self.neutral_user1, voted_for=self.poster)

        # Fast-forward updated_at for voting phase
        Dispute.objects.filter(id=dispute.id).update(
            updated_at=timezone.now() - timedelta(days=10)
        )
        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        # voting -> resolved
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')


