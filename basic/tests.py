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

class DisputeResolutionTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.doer = User.objects.create_user(username='doer', password='password123')
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=1000)

        self.task = Task.objects.create(
            title='Test Escrow Task',
            description='Test description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Partial work done but disagreement on delivery',
            status='open'
        )

        self.client = Client()

    def test_staff_split_dispute_resolution_success(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 70,
            'poster_refund': 30,
            'resolution_notes': 'Doer completed 70% of subtasks.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.doer_payout, 70)
        self.assertEqual(self.dispute.poster_refund, 30)
        self.assertEqual(self.dispute.resolution_notes, 'Doer completed 70% of subtasks.')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.doer_profile.rewards, 570)
        self.assertEqual(self.poster_profile.rewards, 1030)

        doer_ledger = RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').first()
        self.assertIsNotNone(doer_ledger)
        self.assertEqual(doer_ledger.amount, 70)
        self.assertEqual(doer_ledger.task, self.task)

        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, 30)
        self.assertEqual(poster_ledger.task, self.task)

        self.assertTrue(Notification.objects.filter(recipient=self.doer).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_split_resolution_100_0_payout(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 100,
            'poster_refund': 0,
            'resolution_notes': 'Full payout to doer.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 600)
        self.assertEqual(self.poster_profile.rewards, 1000)

        self.assertTrue(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').exists())
        self.assertFalse(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').exists())

    def test_split_resolution_0_100_refund(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 0,
            'poster_refund': 100,
            'resolution_notes': 'Full refund to poster.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 500)
        self.assertEqual(self.poster_profile.rewards, 1100)

        self.assertFalse(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_payout').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').exists())

    def test_unbalanced_split_rejection(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 60,
            'poster_refund': 50,  # 60 + 50 = 110 != 100
            'resolution_notes': 'Invalid total'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.doer_profile.rewards, 500)
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertFalse(RewardLedger.objects.filter(task=self.task).exists())

    def test_negative_split_rejection(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': -10,
            'poster_refund': 110,
            'resolution_notes': 'Negative input'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_non_staff_authorization_denied(self):
        self.client.login(username='regular', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'doer_payout': 50,
            'poster_refund': 50,
            'resolution_notes': 'Unauthorized attempt'
        })
        self.assertEqual(response.status_code, 403)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_cannot_resolve_already_resolved_dispute(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        # First resolution
        self.client.post(url, {
            'doer_payout': 50,
            'poster_refund': 50,
            'resolution_notes': 'First resolution'
        })
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Attempt second resolution
        response = self.client.post(url, {
            'doer_payout': 80,
            'poster_refund': 20,
            'resolution_notes': 'Second resolution attempt'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.assertEqual(self.dispute.doer_payout, 50)
        self.assertEqual(self.doer_profile.rewards, 550)

