from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


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


class PartialDisputeSettlementTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        # Create user profiles
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=500)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=500)

        # Create a task (reward = 100)
        self.task_reward = 100
        # Simulating task creation reservation: poster rewards = 900
        self.poster_profile.rewards -= self.task_reward
        self.poster_profile.save()

        self.task = Task.objects.create(
            title="Test Partial Task",
            description="Task to test partial escrow settlement",
            reward=self.task_reward,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed'
        )

        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.doer)

        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-self.task_reward,
            transaction_type='task_creation',
            description="Reserved for task"
        )

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason="Completed 60% of work before blocker."
        )

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='regular', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'poster_amount': 40,
            'doer_amount': 60,
            'resolution_notes': 'Unauthorized attempt'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.doer_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.doer_profile.rewards, 500)
        self.assertFalse(RewardLedger.objects.filter(transaction_type='dispute_settlement').exists())

    def test_staff_settlement_valid_split_60_40(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'poster_amount': 40,
            'doer_amount': 60,
            'resolution_notes': 'Awarded 60% to doer for work completed and 40% refund to poster.'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.doer_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.dispute.poster_amount, 40)
        self.assertEqual(self.dispute.doer_amount, 60)
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)
        self.assertEqual(self.dispute.resolution_notes, 'Awarded 60% to doer for work completed and 40% refund to poster.')

        # Rewards updated: Poster 900 + 40 = 940, Doer 500 + 60 = 560
        self.assertEqual(self.poster_profile.rewards, 940)
        self.assertEqual(self.doer_profile.rewards, 560)

        # Two RewardLedger entries created with dispute_settlement type
        ledger_entries = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_settlement')
        self.assertEqual(ledger_entries.count(), 2)

        poster_entry = ledger_entries.get(user=self.poster)
        self.assertEqual(poster_entry.amount, 40)

        doer_entry = ledger_entries.get(user=self.doer)
        self.assertEqual(doer_entry.amount, 60)

        # Notifications created for both poster and doer
        poster_notifs = Notification.objects.filter(recipient=self.poster)
        doer_notifs = Notification.objects.filter(recipient=self.doer)
        self.assertTrue(poster_notifs.exists())
        self.assertTrue(doer_notifs.exists())
        self.assertIn('40', poster_notifs.first().message)
        self.assertIn('60', doer_notifs.first().message)

    def test_staff_settlement_percentage_input(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'poster_percent': 70,
            'resolution_notes': '70% to poster, 30% to doer'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.poster_amount, 70)
        self.assertEqual(self.dispute.doer_amount, 30)

    def test_settlement_rejection_invalid_sum(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        # Sum is 110 != 100
        response = self.client.post(url, {
            'poster_amount': 50,
            'doer_amount': 60,
            'resolution_notes': 'Invalid total'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.doer_profile.rewards, 500)

    def test_settlement_rejection_negative_amount(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'poster_amount': -10,
            'doer_amount': 110,
            'resolution_notes': 'Negative poster amount'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.doer_profile.rewards, 500)

    def test_settlement_rejection_already_resolved(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        # Settle once
        self.client.post(url, {
            'poster_amount': 50,
            'doer_amount': 50,
            'resolution_notes': 'First settlement'
        }, follow=True)

        # Try to settle again
        response = self.client.post(url, {
            'poster_amount': 0,
            'doer_amount': 100,
            'resolution_notes': 'Second settlement attempt'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.poster_amount, 50)
        self.assertEqual(self.dispute.doer_amount, 50)
        self.assertEqual(RewardLedger.objects.filter(transaction_type='dispute_settlement').count(), 2)

    def test_staff_settlement_refunds_deposit_bond(self):
        self.dispute.deposit_amount = 50
        self.dispute.escrow_status = 'held'
        self.dispute.save()

        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        self.client.post(url, {
            'poster_amount': 40,
            'doer_amount': 60,
            'resolution_notes': 'Settle and refund bond'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.doer_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')
        # Doer gets 500 + 60 (payout) + 50 (deposit refund) = 610
        self.assertEqual(self.doer_profile.rewards, 610)
        self.assertTrue(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_refund').exists())

