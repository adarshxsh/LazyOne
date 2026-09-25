from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeEvidence, DisputeVote


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Neutral jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=500)
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=500)
        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=500)

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
        self.assertEqual(self.task.deposit_bond_amount, 60)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
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

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'evidence_submission')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check initial evidence record
        evidence = dispute.evidence_records.first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.submitted_by, self.taker)
        self.assertEqual(evidence.description, 'Unreasonable request')

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'withdrawn')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held',
            status='evidence_submission'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

    def test_submit_evidence_and_phase_restrictions(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Initial reason',
            deposit_amount=60,
            escrow_status='held',
            status='evidence_submission'
        )

        # Poster submits evidence
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'title': 'Poster Proof', 'description': 'Delivered work was incomplete.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertTrue(dispute.evidence_records.filter(submitted_by=self.poster).exists())

        # Non-participant tries to submit evidence
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'title': 'Third Party', 'description': 'Unrelated comment'}
        )
        self.assertFalse(dispute.evidence_records.filter(submitted_by=self.juror1).exists())

    def test_peer_jury_voting_and_autoresolution(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Reason',
            deposit_amount=60,
            escrow_status='held',
            status='evidence_submission'
        )

        # Move to voting phase
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('advance_to_voting', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting')

        # Guardrail: Poster attempts to vote on own dispute
        response = self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.poster.id, 'reason': 'Self vote'}
        )
        self.assertFalse(DisputeVote.objects.filter(voter=self.poster).exists())

        # Juror 1 votes for Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'reason': 'Taker provided clear proof.'}
        )
        self.assertEqual(dispute.votes.count(), 1)

        # Duplicate vote by Juror 1
        self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'reason': 'Double vote'}
        )
        self.assertEqual(dispute.votes.count(), 1)

        # Juror 2 votes for Poster
        self.client.login(username='juror2', password='password123')
        self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.poster.id, 'reason': 'Poster requirements were unambiguous.'}
        )

        # Dispute still in voting (2 votes < threshold of 3)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting')

        # Juror 3 votes for Taker (Threshold reached: Taker 2 votes, Poster 1 vote)
        self.client.login(username='juror3', password='password123')
        self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'reason': 'Taker followed instructions.'}
        )

        # Dispute auto-resolves in favor of Taker!
        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.escrow_status, 'refunded')

    def test_sla_command_auto_resolution(self):
        # Dispute 1: Only Poster submitted evidence
        dispute1 = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='No work done',
            deposit_amount=60,
            escrow_status='held',
            status='evidence_submission'
        )
        DisputeEvidence.objects.create(
            dispute=dispute1,
            submitted_by=self.poster,
            description='Evidence from poster'
        )
        dispute1.created_at = timezone.now() - timedelta(days=10)
        dispute1.save()

        # Run SLA command
        call_command('resolve_expired_disputes', days=7)

        dispute1.refresh_from_db()
        self.assertEqual(dispute1.status, 'resolved')
        self.assertEqual(dispute1.task.status, 'cancelled')
