from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.management import call_command
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
        self.assertEqual(dispute.status, 'withdrawn')

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


class DisputeStateTransitionTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username='user1', password='password123')
        self.user2 = User.objects.create_user(username='user2', password='password123')
        UserProfile.objects.create(user=self.user1, rewards=500)
        UserProfile.objects.create(user=self.user2, rewards=500)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="State Transition Task",
            description="Testing states",
            reward=100,
            posted_by=self.user1,
            taken_by=self.user2,
            status='disputed',
            deadline=self.deadline
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.user2,
            reason="State machine test",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

    def test_status_choices_tuple_includes_all_states(self):
        choice_keys = [choice[0] for choice in Dispute.STATUS_CHOICES]
        expected = ['open', 'evidence_submission', 'voting', 'appealed', 'resolved', 'withdrawn']
        for state in expected:
            self.assertIn(state, choice_keys)

    def test_valid_state_transitions_via_transition_to(self):
        # open -> evidence_submission
        self.dispute.transition_to('evidence_submission')
        self.assertEqual(self.dispute.status, 'evidence_submission')
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'evidence_submission')

        # evidence_submission -> voting
        self.dispute.transition_to('voting')
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> appealed
        self.dispute.transition_to('appealed')
        self.assertEqual(self.dispute.status, 'appealed')

        # appealed -> voting
        self.dispute.transition_to('voting')
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> resolved
        self.dispute.transition_to('resolved')
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_state_transition_raises_validation_error(self):
        # open -> appealed is illegal
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('appealed')

        # Verify DB status remained 'open'
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_orm_assignment_and_save_enforces_validation(self):
        # Direct assignment open -> appealed
        self.dispute.status = 'appealed'
        with self.assertRaises(ValidationError):
            self.dispute.save()

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_terminal_states_prohibit_further_transitions(self):
        # Transition open -> resolved (terminal)
        self.dispute.transition_to('resolved')
        self.assertEqual(self.dispute.status, 'resolved')

        # Attempt transition from resolved -> open or voting or appealed or withdrawn
        for target in ['open', 'evidence_submission', 'voting', 'appealed', 'withdrawn']:
            with self.assertRaises(ValidationError):
                self.dispute.transition_to(target)

        # Transition open -> withdrawn (terminal)
        dispute2 = Dispute.objects.create(
            task=Task.objects.create(
                title="Task 2", description="Test", reward=100,
                posted_by=self.user1, taken_by=self.user2, status='disputed', deadline=self.deadline
            ),
            raised_by=self.user2,
            reason="Test withdrawn",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        dispute2.transition_to('withdrawn')
        self.assertEqual(dispute2.status, 'withdrawn')

        for target in ['open', 'evidence_submission', 'voting', 'appealed', 'resolved']:
            with self.assertRaises(ValidationError):
                dispute2.transition_to(target)

    def test_resolve_expired_disputes_command_processes_intermediate_states(self):
        # Set dispute created_at to past expiry threshold (8 days ago)
        Dispute.objects.filter(id=self.dispute.id).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        self.dispute.refresh_from_db()

        # Move to evidence_submission
        self.dispute.transition_to('evidence_submission')
        self.assertEqual(self.dispute.status, 'evidence_submission')

        # Run resolve_expired_disputes management command
        call_command('resolve_expired_disputes', days=7)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

