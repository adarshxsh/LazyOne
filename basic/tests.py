from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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

        dispute = Dispute.objects.get(task=self.task)
        # Advance dispute to resolvable status (voting)
        dispute.status = 'voting'
        dispute.save()

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
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


class DisputeLifecycleTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1000})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Task dispute reason',
            status='open'
        )
        self.client = Client()

    def test_dispute_status_choices(self):
        statuses = dict(Dispute.STATUS_CHOICES)
        expected_keys = [
            'open', 'evidence_submission', 'jury_selection',
            'voting', 'appealed', 'resolved', 'withdrawn'
        ]
        for key in expected_keys:
            self.assertIn(key, statuses)

    def test_valid_transitions(self):
        # open -> evidence_submission
        self.assertTrue(self.dispute.can_transition_to('evidence_submission'))
        self.dispute.transition_to('evidence_submission', save=True)
        self.assertEqual(self.dispute.status, 'evidence_submission')

        # evidence_submission -> jury_selection
        self.assertTrue(self.dispute.can_transition_to('jury_selection'))
        self.dispute.transition_to('jury_selection', save=True)
        self.assertEqual(self.dispute.status, 'jury_selection')

        # jury_selection -> voting
        self.assertTrue(self.dispute.can_transition_to('voting'))
        self.dispute.transition_to('voting', save=True)
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> appealed
        self.assertTrue(self.dispute.can_transition_to('appealed'))
        self.dispute.transition_to('appealed', save=True)
        self.assertEqual(self.dispute.status, 'appealed')

        # appealed -> voting
        self.assertTrue(self.dispute.can_transition_to('voting'))
        self.dispute.transition_to('voting', save=True)
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> resolved
        self.assertTrue(self.dispute.can_transition_to('resolved'))
        self.dispute.transition_to('resolved', save=True)
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_transition_open_to_resolved_raises_validation_error(self):
        self.assertFalse(self.dispute.can_transition_to('resolved'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('resolved')

    def test_invalid_transition_from_terminal_states(self):
        self.dispute.status = 'resolved'
        self.dispute.save()
        self.assertFalse(self.dispute.can_transition_to('open'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('open')

        self.dispute.status = 'withdrawn'
        self.dispute.save()
        self.assertFalse(self.dispute.can_transition_to('open'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('open')

    def test_withdraw_dispute_view(self):
        self.client.login(username='doer', password='password123')
        url = reverse('withdraw_dispute', args=[self.dispute.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')

    def test_complete_task_with_active_open_dispute_fails(self):
        self.client.login(username='poster', password='password123')
        url = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

    def test_complete_task_with_resolvable_dispute_succeeds(self):
        self.dispute.status = 'voting'
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        url = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

    def test_dispute_detail_view_rendering(self):
        self.client.login(username='poster', password='password123')
        url = reverse('dispute_detail', args=[self.dispute.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Open')
        self.assertContains(response, 'Move to Evidence Submission')

    def test_transition_dispute_view(self):
        self.client.login(username='poster', password='password123')
        url = reverse('transition_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'target_state': 'evidence_submission'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'evidence_submission')
