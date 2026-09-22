from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
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
        self.assertEqual(dispute.status, 'evidence_submission')
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
        self.assertEqual(dispute.status, 'cancelled')

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


class DisputeStateMachineTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster2', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker = User.objects.create_user(username='taker2', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.task = Task.objects.create(
            title="State Machine Task",
            description="Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        Conversation.objects.create(task=self.task)
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Testing state machine transitions",
            deposit_amount=50,
            escrow_status='held',
            status='evidence_submission'
        )

    def test_status_choices(self):
        statuses = [choice[0] for choice in Dispute.STATUS_CHOICES]
        expected_statuses = ['evidence_submission', 'voting_period', 'appeal_period', 'resolved', 'cancelled']
        self.assertEqual(statuses, expected_statuses)

    def test_valid_transitions(self):
        # evidence_submission -> voting_period
        self.dispute.advance_to_voting_period()
        self.assertEqual(self.dispute.status, 'voting_period')

        # voting_period -> appeal_period
        self.dispute.advance_to_appeal_period()
        self.assertEqual(self.dispute.status, 'appeal_period')

        # appeal_period -> resolved
        self.dispute.resolve_dispute()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_withdrawal_transition(self):
        # Direct jump evidence_submission -> cancelled (withdrawn)
        self.dispute.cancel_dispute(is_withdrawal=True)
        self.assertEqual(self.dispute.status, 'cancelled')

    def test_withdrawal_to_resolved_transition(self):
        # Direct jump evidence_submission -> resolved with is_withdrawal=True
        self.dispute.resolve_dispute(is_withdrawal=True)
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_transition_raises_validation_error(self):
        # Direct jump evidence_submission -> resolved without is_withdrawal
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('resolved', is_withdrawal=False)

        # Direct jump evidence_submission -> appeal_period
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('appeal_period')

        # Transitioning from terminal state resolved -> voting_period
        self.dispute.resolve_dispute(is_withdrawal=True)
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('voting_period')

    def test_auto_transition_after_24_hours(self):
        # Initially in evidence_submission
        self.assertEqual(self.dispute.status, 'evidence_submission')
        self.assertFalse(self.dispute.check_auto_transition())

        # Set created_at to 25 hours ago
        self.dispute.created_at = timezone.now() - timedelta(hours=25)
        self.dispute.save()

        # check_auto_transition should advance to voting_period
        result = self.dispute.check_auto_transition()
        self.assertTrue(result)
        self.assertEqual(self.dispute.status, 'voting_period')

    def test_dispute_detail_view_renders_lifecycle_and_progress_bar(self):
        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Verify badge color class and progress bar elements in response
        self.assertContains(response, 'bg-blue-500/20 text-blue-300')
        self.assertContains(response, 'Active Lifecycle Phase')
        self.assertContains(response, '1. Evidence Submission')
        self.assertContains(response, '2. Voting Period')
        self.assertContains(response, '3. Appeal Period')
        self.assertContains(response, '4. Resolved')


