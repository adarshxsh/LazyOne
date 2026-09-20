from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.management import call_command
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeEvidence, DisputeVote, DisputeAppeal
from .views.dispute import advance_dispute_phase, settle_dispute


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Community peer reviewer
        self.peer = User.objects.create_user(username='peer_reviewer', password='password123')
        self.peer_profile = UserProfile.objects.create(user=self.peer, rewards=500)

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
        self.assertEqual(dispute.status, 'evidence_phase')
        self.assertEqual(dispute.raised_by, self.taker)

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
        self.assertEqual(dispute.status, 'settled')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

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

        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)

    def test_fsm_invalid_state_transition_raises_validation_error(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='FSM test',
            status='open'
        )

        # Illegal transition: open directly to settled
        with self.assertRaises(ValidationError):
            dispute.transition_to('settled')

        # Sequential transitions
        dispute.transition_to('evidence_phase')
        self.assertEqual(dispute.status, 'evidence_phase')

        # Illegal transition: evidence_phase directly to settled
        with self.assertRaises(ValidationError):
            dispute.transition_to('settled')

        dispute.transition_to('voting_phase')
        self.assertEqual(dispute.status, 'voting_phase')

        dispute.transition_to('appeal_phase')
        self.assertEqual(dispute.status, 'appeal_phase')

        dispute.transition_to('settled')
        self.assertEqual(dispute.status, 'settled')

    def test_evidence_submission_phase(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Evidence testing',
            status='evidence_phase'
        )

        # Disputing party submits evidence
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'description': 'Screenshot evidence of completed work', 'evidence_url': 'https://example.com/screenshot.png'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 1)
        evidence = DisputeEvidence.objects.get(dispute=dispute)
        self.assertEqual(evidence.submitted_by, self.taker)
        self.assertEqual(evidence.evidence_url, 'https://example.com/screenshot.png')

        # Non-participant blocked from submitting evidence
        self.client.login(username='peer_reviewer', password='password123')
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'description': 'Unauthorized evidence'}
        )
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 1)

    def test_voting_phase_and_peer_voting(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Voting test',
            status='voting_phase'
        )

        # Peer reviewer submits vote
        self.client.login(username='peer_reviewer', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'choice': 'taker', 'justification': 'Evidence supports taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertEqual(DisputeVote.objects.filter(dispute=dispute).count(), 1)
        vote = DisputeVote.objects.get(dispute=dispute)
        self.assertEqual(vote.voter, self.peer)
        self.assertEqual(vote.choice, 'taker')

        # Task participant blocked from voting
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'choice': 'taker', 'justification': 'Self vote'}
        )
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute).count(), 1)

    def test_appeal_phase_submission(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Appeal test',
            status='appeal_phase'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[dispute.id]),
            {'reason': 'Fresh justification appealing voting result'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertEqual(DisputeAppeal.objects.filter(dispute=dispute).count(), 1)
        appeal = DisputeAppeal.objects.get(dispute=dispute)
        self.assertEqual(appeal.appellant, self.poster)

    def test_sla_expiration_command_and_settlement(self):
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='SLA expiration test',
            deposit_amount=60,
            escrow_status='held',
            status='appeal_phase'
        )
        dispute.created_at = timezone.now() - timedelta(days=2)
        dispute.save()

        # Peer vote cast for taker
        DisputeVote.objects.create(dispute=dispute, voter=self.peer, choice='taker')

        call_command('resolve_expired_disputes', days=1)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'settled')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400) # 40 remaining + 300 reward + 60 refund
