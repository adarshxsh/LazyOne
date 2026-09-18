from django.test import TestCase, Client, override_settings
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class TakerCollateralEscrowTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.poor_taker = User.objects.create_user(username='poortaker', password='password123')
        self.poor_taker_profile = UserProfile.objects.create(user=self.poor_taker, rewards=5)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

    def test_collateral_required_calculation(self):
        # Default TASK_COLLATERAL_PERCENTAGE is 20
        self.assertEqual(self.task.collateral_required, 20)

        # Test integer floor arithmetic with odd reward amounts
        odd_task = Task.objects.create(
            title='Odd Task',
            description='Odd Description',
            reward=15,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        # 15 * 20 / 100 = 3
        self.assertEqual(odd_task.collateral_required, 3)

        odd_task2 = Task.objects.create(
            title='Odd Task 2',
            description='Odd Description 2',
            reward=17,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        # 17 * 20 / 100 = 3.4 -> 3
        self.assertEqual(odd_task2.collateral_required, 3)

    def test_take_task_insufficient_collateral_rejected(self):
        self.client.login(username='poortaker', password='password123')
        # poor_taker has 5 rewards, but required collateral for 100 reward task is 20
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)

        self.poor_taker_profile.refresh_from_db()
        self.assertEqual(self.poor_taker_profile.rewards, 5)

        # Ensure no ledger deposit created
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='taker_collateral_deposit').exists())

    def test_take_task_success_locks_collateral(self):
        self.client.login(username='taker', password='password123')
        # taker has 100 rewards, required collateral is 20
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        self.assertEqual(self.task.taker_collateral, 20)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20

        # Verify ledger entry
        ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_deposit')
        self.assertEqual(ledger.amount, -20)

    def test_complete_task_refunds_collateral(self):
        # First, taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Now poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # Initial 100 - 20 (claim deposit) + 100 (task reward) + 20 (collateral refund) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        # Verify ledger entries
        refund_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_refund')
        self.assertEqual(refund_ledger.amount, 20)

        completion_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='task_completion')
        self.assertEqual(completion_ledger.amount, 100)

    def test_abandon_task_slashes_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80)

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.task.taker_collateral, 0)

        # Taker profile balance remains 80 (since 20 was deducted on claim and forfeited/slashed now)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80)
        self.assertGreaterEqual(self.taker_profile.rewards, 0)

        # Verify ledger slash entry
        slash_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_slash')
        self.assertEqual(slash_ledger.amount, -20)

        # Verify notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).latest('created_at')
        self.assertIn('abandoned', notification.message)

    def test_mutual_cancellation_refunds_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # 100 - 20 (deposit) + 20 (refund) = 100
        self.assertEqual(self.taker_profile.rewards, 100)

        # Verify ledger refund entry
        refund_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='taker_collateral_refund')
        self.assertEqual(refund_ledger.amount, 20)

    @override_settings(TASK_COLLATERAL_PERCENTAGE=50)
    def test_custom_collateral_percentage_setting(self):
        self.assertEqual(self.task.collateral_required, 50)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.taker_collateral, 50)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50) # 100 - 50

    def test_exact_balance_boundary(self):
        # Set taker rewards to exactly 20
        self.taker_profile.rewards = 20
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taker_collateral, 20)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 0) # 20 - 20 = 0 (exact boundary)

    def test_complete_disputed_task_refunds_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Refresh task state from database
        self.task.refresh_from_db()

        # Taker raises dispute
        from basic.models import Dispute
        Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Issue with task")
        self.task.status = 'disputed'
        self.task.save()

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.taker_collateral, 0)

        self.taker_profile.refresh_from_db()
        # 100 - 20 + 100 + 20 = 200
        self.assertEqual(self.taker_profile.rewards, 200)
