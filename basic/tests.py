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

class DisputeSplitSettlementTests(TestCase):
    def setUp(self):
        # Create regular users (poster and taker)
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')

        # Create profiles
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff_admin', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        # Create task with reward = 100 points
        self.task = Task.objects.create(
            title='Test Task for Dispute',
            description='Test task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )

        # Poster's balance reduced by 100 on creation, simulate 900
        self.poster_profile.rewards = 900
        self.poster_profile.save()

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work partially completed, unable to reach agreement.',
            status='open'
        )

        self.client = Client()

    def test_non_staff_blocked_from_admin_dispute_panel(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('admin_dispute_panel'))
        self.assertEqual(response.status_code, 403)

    def test_non_staff_blocked_from_settling_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'taker_payout': 50, 'poster_refund': 50})
        self.assertEqual(response.status_code, 403)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_can_access_admin_dispute_panel(self):
        self.client.login(username='staff_admin', password='password123')
        response = self.client.get(reverse('admin_dispute_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Task for Dispute')
        self.assertContains(response, '100 Points')

    def test_settlement_rejected_when_split_sum_mismatch(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        # Task reward is 100; submit 60 + 50 = 110
        response = self.client.post(url, {'taker_payout': 60, 'poster_refund': 50}, follow=True)
        self.assertContains(response, 'must equal the total task reward')

        # Verify no changes to balances or status
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.taker_profile.rewards, 500)

    def test_settlement_rejected_when_negative_amounts(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'taker_payout': -10, 'poster_refund': 110}, follow=True)
        self.assertContains(response, 'must be non-negative integers')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_successful_split_settlement_atomic_execution(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        # Settle: 70 points to taker, 30 points refunded to poster (70 + 30 = 100)
        response = self.client.post(url, {'taker_payout': 70, 'poster_refund': 30}, follow=True)
        self.assertContains(response, 'Dispute resolved successfully')

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Check statuses
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Check balances
        self.assertEqual(self.taker_profile.rewards, 500 + 70) # 570
        self.assertEqual(self.poster_profile.rewards, 900 + 30) # 930

        # Check ledger entries
        taker_ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout').first()
        poster_ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()

        self.assertIsNotNone(taker_ledger)
        self.assertEqual(taker_ledger.amount, 70)
        self.assertIn("Dispute settlement payout", taker_ledger.description)

        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, 30)
        self.assertIn("Dispute settlement refund", poster_ledger.description)

    def test_already_resolved_dispute_cannot_be_settled_again(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'taker_payout': 50, 'poster_refund': 50}, follow=True)
        self.assertContains(response, 'already been resolved')

