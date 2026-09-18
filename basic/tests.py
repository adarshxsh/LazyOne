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


class TaskStatusGuardAndDisputeCleanupTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker1_profile = UserProfile.objects.create(user=self.taker1, rewards=1000)

        self.taker2 = User.objects.create_user(username='taker2', password='password123')
        self.taker2_profile = UserProfile.objects.create(user=self.taker2, rewards=1000)

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker1 = Client()
        self.client_taker1.login(username='taker1', password='password123')

        self.client_taker2 = Client()
        self.client_taker2.login(username='taker2', password='password123')

        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        Conversation.objects.create(task=self.task)

    def test_accept_cancellation_rejected_when_not_in_progress(self):
        # Taker1 takes the task
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker1 raises a dispute, putting task status into 'disputed'
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair expectations'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        # Taker1 attempts to accept cancellation while task is in 'disputed' status
        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Verify task remains disputed and dispute was NOT deleted
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=self.task).exists())

    def test_accept_cancellation_success_and_cleans_up_dispute(self):
        # Taker1 takes the task
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))

        # An orphaned/associated dispute record exists on the task
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Previous dispute")

        # Taker1 accepts cancellation while task is in 'in_progress' status
        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task should transition to 'available', taken_by cleared, and dispute record DELETED
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_reassigned_task_allows_new_dispute(self):
        # Taker1 takes task, dispute raised, cancellation accepted and dispute deleted
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Old dispute")
        self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))

        # Taker2 takes the newly available task
        self.client_taker2.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker2)

        # Taker2 raises a new dispute
        response = self.client_taker2.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'New issue by Taker2'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=self.task).exists())

        new_dispute = self.task.dispute
        self.assertEqual(new_dispute.raised_by, self.taker2)
        self.assertEqual(new_dispute.reason, 'New issue by Taker2')

    def test_withdraw_dispute_rejected_when_resolved_or_task_completed(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        self.task.refresh_from_db()
        dispute = self.task.dispute

        # Poster completes task (resolving the dispute and setting task status to 'completed')
        self.client_poster.get(reverse('complete_task', args=[self.task.id]))
        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # Taker1 attempts to withdraw the resolved dispute on completed task
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task state and dispute state must remain completed / resolved
        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

    def test_withdraw_dispute_rejected_when_task_cancelled(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue'})
        dispute = self.task.dispute

        # Task status changed to cancelled
        self.task.status = 'cancelled'
        self.task.save()

        # Taker1 attempts to withdraw dispute
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task status remains cancelled
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_success_when_open_and_disputed(self):
        # Taker1 takes task and raises dispute
        self.client_taker1.get(reverse('take_task', args=[self.task.id]))
        self.client_taker1.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Valid issue'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute

        # Taker1 withdraws the open dispute
        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task status transitions back to 'in_progress' and dispute is resolved and refunded
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
