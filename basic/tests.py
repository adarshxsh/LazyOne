from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, calculate_collateral


class CollateralMechanicsTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create Poster user and profile
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        
        # Create Taker user and profile with 100 points
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 100})

        # Create Sybil user and profile with 0 points
        self.sybil = User.objects.create_user(username='sybil', password='password123')
        self.sybil_profile, _ = UserProfile.objects.get_or_create(user=self.sybil, defaults={'rewards': 0})

    def test_calculate_collateral(self):
        # 20% of reward >= 10
        self.assertEqual(calculate_collateral(100), 20)
        self.assertEqual(calculate_collateral(50), 10)
        
        # 20% of reward < 10, floor of 10 applied
        self.assertEqual(calculate_collateral(40), 10)
        self.assertEqual(calculate_collateral(15), 10)
        
        # Reward < 10, collateral capped at reward
        self.assertEqual(calculate_collateral(8), 8)
        self.assertEqual(calculate_collateral(5), 5)

    def test_sybil_insufficient_collateral_rejection(self):
        # Create a task with 100 reward (requires 20 collateral)
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Sybil has 0 points, cannot afford 20 collateral
        self.client.login(username='sybil', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.sybil_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(self.sybil_profile.rewards, 0)
        self.assertFalse(RewardLedger.objects.filter(user=self.sybil, transaction_type='collateral_lock').exists())

    def test_take_task_collateral_lock(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Collateral is 20 points
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)
        self.assertEqual(task.collateral_amount, 20)
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20

        lock_ledger = RewardLedger.objects.get(user=self.taker, task=task, transaction_type='collateral_lock')
        self.assertEqual(lock_ledger.amount, -20)

    def test_complete_task_restores_collateral_and_awards_reward(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        # Poster completes task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.collateral_amount, 0)
        # Initial 100 - 20 (lock) + 20 (release) + 100 (completion) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=20).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_completion', amount=100).exists())

    def test_accept_cancellation_restores_collateral(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        # Poster requests cancellation
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[task.id]), follow=True)

        # Taker accepts cancellation
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(task.collateral_amount, 0)
        # Taker balance restored to 100 (100 - 20 + 20)
        self.assertEqual(self.taker_profile.rewards, 100)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='collateral_release', amount=20).exists())

    def test_abandon_task_slashes_collateral_and_indemnifies_poster(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )

        # Taker takes task (locking 20 collateral)
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]), follow=True)

        initial_poster_rewards = self.poster_profile.rewards

        # Taker abandons task
        response = self.client.get(reverse('abandon_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(task.collateral_amount, 0)
        # Taker's points remain at 80 (20 collateral slashed/lost)
        self.assertEqual(self.taker_profile.rewards, 80)
        # Poster receives 20 points indemnity
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 20)

        slash_ledger = RewardLedger.objects.get(user=self.poster, task=task, transaction_type='collateral_slashing')
        self.assertEqual(slash_ledger.amount, 20)


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
