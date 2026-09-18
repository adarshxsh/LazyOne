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

        # Taker balance: 40 + 300 (task reward) + 150 (collateral release) + 60 (deposit refund) = 550
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 550)

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


class CollateralLockupAndSlashingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.poor_taker = User.objects.create_user(username='poor_taker', password='password123')
        self.poor_taker_profile = UserProfile.objects.create(user=self.poor_taker, rewards=20)

        self.client = Client()

    def test_collateral_amount_calculation(self):
        task1 = Task.objects.create(
            title='Test Task 1', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        task2 = Task.objects.create(
            title='Test Task 2', description='Desc', reward=15,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.assertEqual(task1.collateral_amount, 50)
        self.assertEqual(task2.collateral_amount, 7)

    def test_take_task_insufficient_collateral_fails(self):
        task = Task.objects.create(
            title='High Reward Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='poor_taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.poor_taker_profile.refresh_from_db()

        # Task should remain available and untaken
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        # Balance should remain unchanged
        self.assertEqual(self.poor_taker_profile.rewards, 20)
        # No collateral lock entry in RewardLedger
        self.assertFalse(RewardLedger.objects.filter(user=self.poor_taker, transaction_type='collateral_lock').exists())

    def test_take_task_sufficient_collateral_locks_points(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Task assigned and status in_progress
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)
        # 50 points locked from 100 available -> 50 remaining
        self.assertEqual(self.taker_profile.rewards, 50)

        # RewardLedger collateral lock entry exists
        ledger_entry = RewardLedger.objects.get(
            user=self.taker, task=task, transaction_type='collateral_lock'
        )
        self.assertEqual(ledger_entry.amount, -50)

    def test_complete_task_releases_collateral_and_awards_reward(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        # Taker starts with 100, -50 lock + 100 reward + 50 collateral release = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_completion', amount=100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=50).exists())

    def test_accept_cancellation_refunds_collateral(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        # Taker collateral refunded: 50 remaining + 50 refunded = 100
        self.assertEqual(self.taker_profile.rewards, 100)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_refund', amount=50).exists())

    def test_abandon_task_slashes_collateral_to_poster(self):
        task = Task.objects.create(
            title='Standard Task', description='Desc', reward=100,
            posted_by=self.poster, deadline=timezone.now() + timedelta(days=1)
        )
        # Taker claims task (taker balance 100 -> 50)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Taker abandons task
        self.client.get(reverse('abandon_task', args=[task.id]))

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Task is back to available and unassigned
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Taker balance remains 50 (the locked 50 was slashed)
        self.assertEqual(self.taker_profile.rewards, 50)

        # Poster receives the slashed 50 collateral points (1000 initial + 50 slashed = 1050)
        self.assertEqual(self.poster_profile.rewards, 1050)

        # Slashing penalty transaction entries exist in RewardLedger
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, task=task, transaction_type='collateral_slash', amount=50).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_slash', amount=-50).exists())
