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

        # Taker balance: 40 + 300 (collateral release) + 300 (task reward) + 60 (deposit refund) = 700
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

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


class CollateralAndSlashingEngineTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster user and profile
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.poster_profile.rewards = 1500
        self.poster_profile.save()

        # Create taker user and profile
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.taker_profile.rewards = 1500
        self.taker_profile.save()

        # Create an entry-level task (reward = 100 points)
        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Sample Task',
            description='Task description',
            reward=100,
            posted_by=self.poster,
            deadline=self.deadline,
            status='available'
        )
        # Deduct poster points for task creation to simulate add_task view logic
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: 'Sample Task'"
        )

    def test_take_task_insufficient_points_fails(self):
        # Set taker balance below required collateral (100)
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertEqual(self.taker_profile.rewards, 50)
        self.assertFalse(
            RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_lock').exists()
        )

    def test_take_task_sufficient_points_locks_collateral(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('take_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)
        # Taker balance should be reduced by 100 (1500 - 100 = 1400)
        self.assertEqual(self.taker_profile.rewards, 1400)

        ledger_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_lock')
        self.assertEqual(ledger_entry.amount, -100)
        self.assertEqual(ledger_entry.task, self.task)

    def test_task_completion_releases_collateral_and_pays_reward(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        # Taker profile gets 100 (collateral release) + 100 (task reward) = 1600 total (1400 + 200)
        self.assertEqual(self.taker_profile.rewards, 1600)

        collateral_release_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_release')
        self.assertEqual(collateral_release_entry.amount, 100)

        task_completion_entry = RewardLedger.objects.get(user=self.taker, transaction_type='task_completion')
        self.assertEqual(task_completion_entry.amount, 100)

    def test_task_abandonment_slashes_collateral_and_compensates_poster(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Taker abandons task
        response = self.client.post(reverse('abandon_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance remains 1400 (lost 100 pts collateral)
        self.assertEqual(self.taker_profile.rewards, 1400)

        # Poster balance restored by 100 pts compensation (1400 + 100 = 1500)
        self.assertEqual(self.poster_profile.rewards, 1500)

        taker_slash_entry = RewardLedger.objects.get(user=self.taker, transaction_type='collateral_slash')
        self.assertEqual(taker_slash_entry.amount, -100)

        poster_slash_entry = RewardLedger.objects.get(user=self.poster, transaction_type='collateral_slash')
        self.assertEqual(poster_slash_entry.amount, 100)

    def test_accept_cancellation_releases_taker_collateral(self):
        # Taker claims task
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('take_task', args=[self.task.id]))

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('request_cancellation', args=[self.task.id]))

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Poster points refunded: 1400 + 100 = 1500
        self.assertEqual(self.poster_profile.rewards, 1500)

        # Taker collateral released: 1400 + 100 = 1500
        self.assertEqual(self.taker_profile.rewards, 1500)

        self.assertTrue(
            RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_release').exists()
        )
