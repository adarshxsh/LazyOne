from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification


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


class StaffDisputeArbitrationTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.superuser = User.objects.create_superuser(username='admin', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        # User profiles are created or updated
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})

        # Create task (poster reserved 100 points)
        self.poster_profile.rewards = 900
        self.poster_profile.save()
        self.task = Task.objects.create(
            title="Test Task",
            description="Task for testing disputes",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work submitted but not accepted",
            status='open'
        )

        self.client = Client()

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'taker_win', 'resolution_notes': 'Taker did full work'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_resolve_dispute_taker_win(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'taker_win', 'resolution_notes': 'Taker provided full proof.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'taker_win')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1100)  # 1000 + 100

        # Check ledger
        ledger = RewardLedger.objects.get(user=self.taker, transaction_type='arbitration_award')
        self.assertEqual(ledger.amount, 100)

        # Check notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_resolve_dispute_poster_win(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'poster_win', 'resolution_notes': 'Task incomplete.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'poster_win')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1000)  # 900 + 100 refund

        ledger = RewardLedger.objects.get(user=self.poster, transaction_type='arbitration_refund')
        self.assertEqual(ledger.amount, 100)

    def test_staff_resolve_dispute_split(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'split', 'resolution_notes': 'Half work done.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'split')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1050)  # 1000 + 50
        self.assertEqual(self.poster_profile.rewards, 950)   # 900 + 50

    def test_submit_appeal_success(self):
        # First resolve dispute
        self.client.login(username='staff', password='password123')
        self.client.post(
            reverse('resolve_dispute_admin', args=[self.dispute.id]),
            {'resolution_type': 'poster_win', 'resolution_notes': 'Poster wins initially'}
        )

        # Participant submits appeal
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'I have additional screenshot proof.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'I have additional screenshot proof.')

    def test_submit_appeal_past_7_days_fails(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Late appeal'}
        )
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.appeal_status)

    def test_single_appeal_limit(self):
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_appeal', args=[self.dispute.id]),
            {'appeal_reason': 'Second appeal attempt'}
        )
        # Should stay pending by taker
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appealed_by, self.taker)

    def test_resolve_appeal_uphold(self):
        # Setup pending appeal
        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.appeal_reason = 'More proof'
        self.dispute.save()

        self.client.login(username='admin', password='password123')
        response = self.client.post(
            reverse('resolve_appeal_admin', args=[self.dispute.id]),
            {'appeal_decision': 'uphold', 'appeal_notes': 'Initial decision was correct.'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'upheld')
        self.assertEqual(self.dispute.appeal_resolved_by, self.superuser)

    def test_resolve_appeal_overturn_poster_win(self):
        # Initial: poster won -> poster gained 100 (balance 1000), taker 0 (balance 1000)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.dispute.status = 'resolved'
        self.dispute.resolution_type = 'poster_win'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.appealed_by = self.taker
        self.dispute.appeal_reason = 'Misunderstood evidence'
        self.dispute.save()

        self.client.login(username='admin', password='password123')
        response = self.client.post(
            reverse('resolve_appeal_admin', args=[self.dispute.id]),
            {'appeal_decision': 'overturn', 'appeal_notes': 'Taker actually finished requirement.'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'overturned')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.poster_profile.rewards, 900)  # 1000 - 100
        self.assertEqual(self.taker_profile.rewards, 1100)  # 1000 + 100

        # Check ledger adjustments
        poster_adj = RewardLedger.objects.get(user=self.poster, transaction_type='appeal_adjustment')
        taker_adj = RewardLedger.objects.get(user=self.taker, transaction_type='appeal_adjustment')
        self.assertEqual(poster_adj.amount, -100)
        self.assertEqual(taker_adj.amount, 100)

