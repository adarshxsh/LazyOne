from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification


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


class ProportionalDisputeSettlementTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Create Taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Create Staff
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff, rewards=1000)

        # Create Task (Reward 100)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )

        # Poster's initial points reserved during task creation
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-100,
            transaction_type='task_creation', description="Reserved for task: 'Test Task'"
        )

        # Create Dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Partial work completed before dispute'
        )

    def test_reward_ledger_and_dispute_model_fields(self):
        """Verify model choices and settlement fields exist."""
        transaction_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('partial_payout', transaction_types)
        self.assertIn('partial_refund', transaction_types)

        self.dispute.taker_payout_amount = 60
        self.dispute.poster_refund_amount = 40
        self.dispute.resolved_by = self.staff
        self.dispute.resolution_notes = "60% work done"
        self.dispute.save()

        refreshed = Dispute.objects.get(id=self.dispute.id)
        self.assertEqual(refreshed.taker_payout_amount, 60)
        self.assertEqual(refreshed.poster_refund_amount, 40)
        self.assertEqual(refreshed.resolved_by, self.staff)
        self.assertEqual(refreshed.resolution_notes, "60% work done")

    def test_staff_proportional_settlement_success(self):
        """Staff successfully settles a dispute with explicit points split."""
        self.client.login(username='staff', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'taker_payout_amount': '60',
            'poster_refund_amount': '40',
            'resolution_notes': 'Taker completed 60% of agreed work.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # Verify UserProfile reward balances
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.taker_profile.rewards, 1060) # 1000 + 60
        self.assertEqual(self.poster_profile.rewards, 940)   # 900 + 40
        # Financial ledger balance check: 1060 + 940 == 2000 (total initial rewards)

        # Verify Dispute status and fields
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.taker_payout_amount, 60)
        self.assertEqual(self.dispute.poster_refund_amount, 40)
        self.assertEqual(self.dispute.resolved_by, self.staff)
        self.assertEqual(self.dispute.resolution_notes, 'Taker completed 60% of agreed work.')

        # Verify Task status
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Verify RewardLedger records
        payout_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='partial_payout')
        refund_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='partial_refund')

        self.assertEqual(payout_ledger.amount, 60)
        self.assertEqual(refund_ledger.amount, 40)
        self.assertIn('Test Task', payout_ledger.description)
        self.assertIn(str(self.dispute.id), payout_ledger.description)

        # Verify Notifications
        taker_notif = Notification.objects.get(recipient=self.taker)
        poster_notif = Notification.objects.get(recipient=self.poster)

        self.assertIn('60 points payout', taker_notif.message)
        self.assertIn('40 points refund', poster_notif.message)

    def test_percentage_settlement_split(self):
        """Staff settles dispute using percentage split."""
        self.client.login(username='staff', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'split_type': 'percentage',
            'taker_percentage': '75',
            'resolution_notes': '75% completion'
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.taker_profile.rewards, 1075)
        self.assertEqual(self.poster_profile.rewards, 925)

    def test_non_staff_authorization_blocked(self):
        """Non-staff user cannot execute partial settlement."""
        self.client.login(username='taker', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'taker_payout_amount': '60',
            'poster_refund_amount': '40',
            'resolution_notes': 'Unauthorized attempt'
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # Balances and statuses remain unchanged
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(self.taker_profile.rewards, 1000)
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.dispute.status, 'open')

    def test_invalid_settlement_sum_aborts(self):
        """Settlement amounts that do not equal task reward abort transaction."""
        self.client.login(username='staff', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        # Sum is 110 != task.reward (100)
        response = self.client.post(url, {
            'taker_payout_amount': '70',
            'poster_refund_amount': '40',
            'resolution_notes': 'Invalid sum'
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # Verify no changes executed
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(self.taker_profile.rewards, 1000)
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.dispute.status, 'open')

    def test_negative_settlement_amounts_aborts(self):
        """Negative payout/refund amounts are rejected."""
        self.client.login(username='staff', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'taker_payout_amount': '-10',
            'poster_refund_amount': '110',
            'resolution_notes': 'Negative input'
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_cannot_resettle_already_resolved_dispute(self):
        """Resolved disputes cannot be partially settled again."""
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='staff', password='password123')
        url = reverse('settle_partial_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'taker_payout_amount': '50',
            'poster_refund_amount': '50',
            'resolution_notes': 'Re-settle attempt'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1000)
