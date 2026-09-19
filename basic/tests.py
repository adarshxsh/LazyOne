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
        # Set taker rewards to 70 (less than 60 deposit + 25 filing fee = 85 required)
        self.taker_profile.rewards = 70
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
        self.assertEqual(self.taker_profile.rewards, 70)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60, filing fee is 25. Taker balance was 100 -> now 15
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 15)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.filing_fee, 25)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check deposit ledger
        deposit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(deposit_ledger)
        self.assertEqual(deposit_ledger.amount, -60)

        # Check filing fee ledger
        fee_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_filing_fee').first()
        self.assertIsNotNone(fee_ledger)
        self.assertEqual(fee_ledger.amount, -25)

    def test_withdraw_dispute_success(self):
        # First raise dispute (100 - 60 deposit - 25 filing fee = 15 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.withdrawal_penalty, 15)  # 25% of 60 = 15
        self.assertEqual(dispute.net_refund, 45)           # 60 - 15 = 45

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance restored with net refund: 15 + 45 = 60
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 60)

        # Poster receives penalty compensation: 1000 + 15 = 1015
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1015)

        # Check refund ledger
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 45)

        # Check forfeit ledger for poster
        forfeit_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)
        self.assertEqual(forfeit_ledger.amount, 15)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 + filing fee 25 deducted from 100 -> 15 left)
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

        # Taker balance: 15 + 300 (task reward) + 60 (deposit refund) = 375
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 375)

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
            filing_fee=25,
            escrow_status='held'
        )
        self.taker_profile.rewards = 15
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 15 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 15)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)

    def test_withdrawal_penalty_minimum_applies(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test min penalty',
            deposit_amount=30,  # 25% of 30 is 7.5 (ceil 8), but min 10 applies
            filing_fee=25,
            escrow_status='held'
        )
        self.assertEqual(dispute.withdrawal_penalty, 10)
        self.assertEqual(dispute.net_refund, 20)

    def test_dispute_detail_view_renders_filing_fee_and_withdrawal_modal(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Testing dispute detail view'}
        )
        dispute = Dispute.objects.get(task=self.task)

        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Non-Refundable Filing Fee:')
        self.assertContains(response, '25 points')
        self.assertContains(response, 'Withdraw Dispute')
        self.assertContains(response, 'Non-Refundable Withdrawal Penalty:')
        self.assertContains(response, '-15 points')
        self.assertContains(response, '+45 points')

