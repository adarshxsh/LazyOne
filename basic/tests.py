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


class StaffDisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.staff_user = User.objects.create_superuser(username='admin_staff', password='adminpassword')
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=2000)

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title="Arbitration Task",
            description="Arbitration Description",
            reward=400,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        Conversation.objects.create(task=self.task)

        # Raise dispute as taker (deposit bond = max(50, ceil(400 * 0.20)) = 80)
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work submitted but poster uncooperative.'}
        )
        self.dispute = Dispute.objects.get(task=self.task)

    def test_staff_arbitration_favor_taker(self):
        self.client.login(username='admin_staff', password='adminpassword')
        response = self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'taker', 'explanation': 'Taker provided complete evidence.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'arbitrated')
        self.assertEqual(self.dispute.arbitrated_by, self.staff_user)
        self.assertIn('Taker', self.dispute.arbitration_ruling)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: initial 500 - 80 (deposit) + 400 (reward) + 80 (deposit refund) = 900
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 900)

        # Ledger check for dispute_arbitration_payout
        payout_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_arbitration_payout').first()
        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.amount, 400)

    def test_staff_arbitration_favor_poster(self):
        self.client.login(username='admin_staff', password='adminpassword')
        response = self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'poster', 'explanation': 'Work incomplete.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'arbitrated')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Poster balance: 1000 + 400 (task refund) + 80 (forfeited deposit bond from taker) = 1480
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1480)

        payout_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_arbitration_payout').first()
        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.amount, 400)

    def test_non_staff_cannot_arbitrate(self):
        self.client.login(username='poster_user', password='password123')
        response = self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'poster', 'explanation': 'Poster ruling.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_submit_appeal_workflow_and_resolution(self):
        # 1. Staff arbitrates
        self.client.login(username='admin_staff', password='adminpassword')
        self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'poster', 'explanation': 'Initial ruling for poster.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'arbitrated')

        # 2. Taker appeals within 72 hours
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Staff overlooked proof of work attachment.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'under_appeal')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'Staff overlooked proof of work attachment.')

        # 3. Staff resolves appeal (reverses ruling in favor of taker)
        self.client.login(username='admin_staff', password='adminpassword')
        response = self.client.post(
            reverse('resolve_appeal', args=[self.dispute.id]),
            {'decision': 'favor_taker', 'notes': 'Overlooked evidence accepted on appeal.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Ledger check for dispute_appeal_adjustment
        adj_ledger = RewardLedger.objects.filter(transaction_type='dispute_appeal_adjustment')
        self.assertTrue(adj_ledger.exists())

    def test_submit_appeal_after_72_hours_rejected(self):
        # Staff arbitrates
        self.client.login(username='admin_staff', password='adminpassword')
        self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'poster', 'explanation': 'Initial ruling.'}
        )
        self.dispute.refresh_from_db()

        # Set arbitrated_at to 80 hours ago
        self.dispute.arbitrated_at = timezone.now() - timedelta(hours=80)
        self.dispute.save()

        # Taker attempts to appeal
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Late appeal.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'arbitrated')

    def test_non_participant_cannot_appeal(self):
        # Staff arbitrates
        self.client.login(username='admin_staff', password='adminpassword')
        self.client.post(
            reverse('dispute_arbitrate', args=[self.dispute.id]),
            {'ruling': 'poster', 'explanation': 'Initial ruling.'}
        )
        self.dispute.refresh_from_db()

        # Other user attempts to appeal
        self.client.login(username='other_user', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Interloper appeal.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'arbitrated')


