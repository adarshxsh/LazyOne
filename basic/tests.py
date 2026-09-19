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

    def test_accept_cancellation_disputed_task_blocked(self):
        # Poster requests cancellation
        self.task.cancellation_requested = True
        self.task.save()

        # Taker raises a dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        poster_rewards_before = self.poster_profile.rewards

        # Taker attempts to accept cancellation while task is disputed
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))

        # Task and dispute state must remain unchanged
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.escrow_status, 'held')

        # Poster reward balance should remain unchanged
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, poster_rewards_before)

        # Message error check
        messages_list = list(response.context['messages'])
        self.assertTrue(any("Cannot accept cancellation while a dispute is active" in str(m) for m in messages_list))

    def test_accept_cancellation_non_in_progress_blocked(self):
        # Task is completed and cancellation_requested was set
        self.task.status = 'completed'
        self.task.cancellation_requested = True
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        messages_list = list(response.context['messages'])
        self.assertTrue(any("Cancellation can only be accepted" in str(m) for m in messages_list))

    def test_withdraw_dispute_already_resolved_blocked(self):
        # Raise dispute then complete task (resolves dispute)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[self.task.id]))

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker tries to withdraw resolved dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))

        # Task should remain completed, NOT reverted to in_progress
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        messages_list = list(response.context['messages'])
        self.assertTrue(any("no longer active or the task is not in disputed status" in str(m) for m in messages_list))

    def test_withdraw_dispute_non_disputed_task_blocked(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Open dispute manual',
            deposit_amount=60,
            escrow_status='held',
            status='open'
        )
        # Task is in_progress, not disputed
        self.assertEqual(self.task.status, 'in_progress')

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        messages_list = list(response.context['messages'])
        self.assertTrue(any("no longer active or the task is not in disputed status" in str(m) for m in messages_list))


