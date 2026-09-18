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

class PartialSettleDisputeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='arbitrator', password='password123', is_staff=True)
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1500)
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=1500)

        # Create task with 1000 points reward (poster balance becomes 500)
        self.poster_profile.rewards -= 1000
        self.poster_profile.save()
        self.task = Task.objects.create(
            title="Design Logo",
            description="Create a modern logo",
            reward=1000,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-1000,
            transaction_type='task_creation', description="Reserved for task: 'Design Logo'"
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work partially completed",
            status='open'
        )

    def test_partial_settle_dispute_percentage_split_50_50(self):
        """Arbitrator settles with a 50/50 percentage split."""
        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])
        
        response = self.client.post(url, {'percentage': '50'})
        self.assertEqual(response.status_code, 302)

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Initial poster balance was 500 + 500 refund = 1000
        self.assertEqual(self.poster_profile.rewards, 1000)
        # Initial taker balance was 1500 + 500 payout = 2000
        self.assertEqual(self.taker_profile.rewards, 2000)

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Check ledger entries
        payout_ledger = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='partial_dispute_payout'
        ).first()
        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.amount, 500)

        refund_ledger = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='partial_dispute_refund'
        ).first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 500)

    def test_partial_settle_dispute_custom_percentage_json(self):
        """JSON request settling with 75% payout to taker."""
        self.client.login(username='poster', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(
            url,
            data={'percentage': 75},
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['payout_amount'], 750)
        self.assertEqual(data['refund_amount'], 250)

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.poster_profile.rewards, 500 + 250)
        self.assertEqual(self.taker_profile.rewards, 1500 + 750)

    def test_partial_settle_dispute_explicit_point_split(self):
        """Settling with explicit point split (600 payout, 400 refund)."""
        self.client.login(username='taker', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'payout_amount': '600',
            'refund_amount': '400'
        })
        self.assertEqual(response.status_code, 302)

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.poster_profile.rewards, 500 + 400)
        self.assertEqual(self.taker_profile.rewards, 1500 + 600)

    def test_partial_settle_dispute_integer_balance_conservation(self):
        """Fractional percentage test ensuring balance conservation."""
        self.task.reward = 100
        self.task.save()

        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'percentage': '33.33'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # 33 payout + 67 refund = 100
        self.assertEqual(data['payout_amount'], 33)
        self.assertEqual(data['refund_amount'], 67)
        self.assertEqual(data['payout_amount'] + data['refund_amount'], 100)

    def test_partial_settle_dispute_invalid_negative_percentage(self):
        """Reject negative percentage values."""
        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'percentage': '-10'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_partial_settle_dispute_exceeding_total_escrow(self):
        """Reject split exceeding total escrow amount."""
        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'percentage': '150'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)

    def test_partial_settle_dispute_point_sum_mismatch(self):
        """Reject explicit split where payout + refund != reward."""
        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {
            'payout_amount': '600',
            'refund_amount': '600'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)

    def test_partial_settle_dispute_unauthorized_user(self):
        """Reject settlement attempt by unrelated user."""
        self.client.login(username='other', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'percentage': '50'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_partial_settle_dispute_already_resolved(self):
        """Cannot settle an already resolved dispute."""
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='arbitrator', password='password123')
        url = reverse('partial_settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'percentage': '50'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
