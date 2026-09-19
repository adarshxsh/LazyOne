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

        # Taker balance: 40 + 300 (task reward) + 60 (dispute deposit refund) + 60 (taker collateral refund) = 460
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 460)

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


class TakerCollateralStakingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster2', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker2', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Available Task",
            description="Task Description",
            reward=300,  # deposit_bond_amount = 60
            posted_by=self.poster,
            status='available',
            deadline=self.deadline
        )

    def test_take_task_insufficient_rewards(self):
        self.taker_profile.rewards = 40  # Less than required 60
        self.taker_profile.save()

        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral_deposit').exists())

    def test_take_task_success(self):
        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)

        # Balance reduced by 60: 200 - 60 = 140
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_complete_task_refunds_collateral_and_reward(self):
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.client.login(username='poster2', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: 140 + 300 (reward) + 60 (collateral refund) = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        completion_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion').first()
        self.assertIsNotNone(completion_ledger)
        self.assertEqual(completion_ledger.amount, 300)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_accept_cancellation_refunds_collateral(self):
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster2', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Poster gets task reward refunded: 1000 + 300 = 1300
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

        # Taker gets collateral bond refunded: 140 + 60 = 200
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 200)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_abandon_task_slashes_collateral_to_poster(self):
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance remains 140 (60 forfeited)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Poster balance increases by 60: 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Dual RewardLedger entries for taker_collateral_slashed
        poster_slashed = RewardLedger.objects.filter(user=self.poster, transaction_type='taker_collateral_slashed').first()
        self.assertIsNotNone(poster_slashed)
        self.assertEqual(poster_slashed.amount, 60)

        taker_slashed = RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral_slashed').first()
        self.assertIsNotNone(taker_slashed)
        self.assertEqual(taker_slashed.amount, 0)


