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


class ProRataDisputeSettlementTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        # Create task: reward = 300
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Pro Rata Task",
            description="Task for pro rata testing",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Raise dispute (deposit bond = 60, taker balance was 200 -> 140 left)
        self.taker_profile.rewards = 140
        self.taker_profile.save()
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Partial completion dispute",
            deposit_amount=60,
            escrow_status='held',
            status='open'
        )

    def test_resolve_dispute_60_40_split(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {
                'percent': '60',
                'deposit_action': 'refund',
                'settlement_notes': '60% work completed satisfactorily'
            }
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        # Check task & dispute status
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.settlement_taker_share_percent, 60)
        self.assertEqual(self.dispute.settlement_notes, '60% work completed satisfactorily')
        self.assertEqual(self.dispute.escrow_status, 'refunded')

        # Check point distributions
        # Taker: 140 (initial remaining) + math.floor(300 * 0.6) = 180 + 60 (refund) = 380
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 380)

        # Poster: 1000 + (300 - 180) = 1120
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1120)

        # Check RewardLedger entries
        payout_ledger = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_partial_payout'
        ).first()
        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.amount, 180)

        refund_ledger = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='dispute_partial_refund'
        ).first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 120)

        # Financial integrity: sum equals reward
        self.assertEqual(payout_ledger.amount + refund_ledger.amount, self.task.reward)

    def test_resolve_dispute_custom_amounts(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {
                'doer_amount': '200',
                'poster_amount': '100',
                'deposit_action': 'refund'
            }
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.doer_amount, 200)
        self.assertEqual(self.dispute.poster_amount, 100)
        self.assertEqual(self.dispute.status, 'resolved')

    def test_resolve_dispute_preset_split(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {
                'split_preset': '50_50',
                'deposit_action': 'refund'
            }
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.doer_amount, 150)
        self.assertEqual(self.dispute.poster_amount, 150)

    def test_resolve_dispute_forfeit_deposit(self):
        self.client.login(username='poster', password='password123')
        self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {
                'percent': '30',
                'deposit_action': 'forfeit',
                'settlement_notes': '30% work done, deposit forfeited'
            }
        )

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.escrow_status, 'forfeited')

        # Taker: 140 + floor(300 * 0.3) = 140 + 90 = 230
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 230)

        # Poster: 1000 + (300 - 90) + 60 (forfeited bond) = 1270
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1270)

    def test_resolve_dispute_unauthorized_user(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'percent': '50'}
        )

        self.assertRedirects(response, reverse('home'))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_resolve_dispute_invalid_percentage(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'percent': '150'}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_resolve_dispute_sum_mismatch(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {
                'doer_amount': '100',
                'poster_amount': '100'  # Sum 200 != task.reward 300
            }
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_complete_task_with_percentage_split(self):
        task2 = Task.objects.create(
            title="Partial Completion Task",
            description="Task description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        self.poster_profile.rewards = 800  # paid 200 out of 1000
        self.poster_profile.save()
        self.taker_profile.rewards = 100
        self.taker_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('complete_task', args=[task2.id]),
            {'percent': '70'}
        )
        self.assertRedirects(response, reverse('my_tasks'))

        task2.refresh_from_db()
        self.assertEqual(task2.status, 'completed')

        # Taker gets 70% of 200 = 140 => 100 + 140 = 240
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 240)

        # Poster refunded 30% of 200 = 60 => 800 + 60 = 860
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 860)

