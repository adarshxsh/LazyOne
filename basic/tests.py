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

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) + 60 (taker collateral refund) = 460
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


class TakerCollateralAndSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        # Poster posted a 300 reward task: 1000 initial - 300 reserved = 700 remaining
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=700)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.staff = User.objects.create_user(username='staff_admin', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Available Task",
            description="Task Description",
            reward=300,
            posted_by=self.poster,
            status='available',
            deadline=self.deadline
        )

    def test_take_task_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker)

        # Collateral for reward 300 is 60. Taker balance: 200 - 60 = 140
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='taker_collateral').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_take_task_insufficient_rewards(self):
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_take_own_task_blocked(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('take_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')

    def test_complete_task_returns_reward_and_collateral(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: 140 + 300 (reward) + 60 (collateral refund) = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_accept_cancellation_refunds_collateral(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.client.login(username='poster', password='password123')
        self.client.get(reverse('request_cancellation', args=[self.task.id]))

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker collateral refunded: 140 + 60 = 200
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 200)

        # Poster task reward refunded: 700 + 300 = 1000
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

    def test_abandon_task_slashes_collateral(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance remains 140 (collateral 60 slashed)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Poster receives 60 collateral compensation: 700 + 60 = 760
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 760)

        slashed_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='collateral_slashed').first()
        self.assertIsNotNone(slashed_ledger)

    def test_staff_resolve_dispute_favour_poster_slashes_collateral(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        # Poster raises dispute (deposit bond = 60)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Taker failed to deliver'})

        dispute = Dispute.objects.get(task=self.task)

        # Staff resolves in favor of poster
        self.client.login(username='staff_admin', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'decision': 'favour_poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance remains 140 (60 collateral slashed)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Poster gets: 700 - 60 (dispute bond) + 300 (reward refund) + 60 (slashed collateral compensation) + 60 (dispute bond refund) = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

    def test_staff_resolve_dispute_favour_taker(self):
        # Give taker 200 rewards so they can take task (60) and raise dispute (60)
        self.taker_profile.rewards = 200
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id])) # balance -> 140
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair demands'}) # balance -> 80

        dispute = Dispute.objects.get(task=self.task)

        # Staff resolves in favor of taker
        self.client.login(username='staff_admin', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'decision': 'favour_taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker gets: 80 + 300 (reward) + 60 (collateral refund) + 60 (dispute deposit refund) = 500
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Reason'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'decision': 'favour_poster'})
        self.assertRedirects(response, reverse('home'))

    def test_resolve_expired_disputes_command(self):
        from django.core.management import call_command
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Old dispute'})

        dispute = Dispute.objects.get(task=self.task)
        # Backdate dispute creation
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

