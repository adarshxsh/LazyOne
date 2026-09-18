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


class DisputeAndTaskStatusCheckTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker2 = User.objects.create_user(username='taker2', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.taker2, rewards=1000)

    def _create_task_with_conversation(self, **kwargs):
        task = Task.objects.create(**kwargs)
        if task.taken_by:
            conv = Conversation.objects.create(task=task)
            conv.participants.add(task.posted_by, task.taken_by)
        return task

    def test_accept_cancellation_success_and_cleans_up_dispute(self):
        task = self._create_task_with_conversation(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            cancellation_requested=True
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='open'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_accept_cancellation_rejects_non_in_progress_tasks(self):
        self.client.login(username='taker', password='password123')

        for invalid_status in ['disputed', 'completed', 'cancelled']:
            task = self._create_task_with_conversation(
                title=f'Test Task {invalid_status}',
                description='Test Description',
                reward=100,
                posted_by=self.poster,
                taken_by=self.taker,
                status=invalid_status,
                cancellation_requested=True
            )

            response = self.client.get(reverse('accept_cancellation', args=[task.id]))
            self.assertEqual(response.status_code, 404)
            task.refresh_from_db()
            self.assertEqual(task.status, invalid_status)

    def test_withdraw_dispute_status_check_success(self):
        task = self._create_task_with_conversation(
            title='Disputed Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='open',
            deposit_amount=50,
            escrow_status='held'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertRedirects(response, reverse('my_tasks'))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')

    def test_withdraw_dispute_fails_if_dispute_not_open(self):
        task = self._create_task_with_conversation(
            title='Disputed Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertEqual(response.status_code, 404)

    def test_withdraw_dispute_cannot_modify_completed_or_cancelled_tasks(self):
        self.client.login(username='taker', password='password123')

        for task_status in ['completed', 'cancelled']:
            task = self._create_task_with_conversation(
                title=f'Task {task_status}',
                description='Test Description',
                reward=100,
                posted_by=self.poster,
                taken_by=self.taker,
                status=task_status
            )
            dispute = Dispute.objects.create(
                task=task,
                raised_by=self.taker,
                reason='Test reason',
                status='open'
            )

            response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
            self.assertEqual(response.status_code, 404)

            task.refresh_from_db()
            self.assertEqual(task.status, task_status)
            self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_raise_dispute_on_reopened_task(self):
        task = self._create_task_with_conversation(
            title='Reopened Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            cancellation_requested=True
        )
        old_dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Old dispute',
            status='open'
        )

        # Taker accepts cancellation which resets task to available and deletes dispute
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('accept_cancellation', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertFalse(Dispute.objects.filter(id=old_dispute.id).exists())

        # Second taker takes the task
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker2)

        # Second taker raises a dispute
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'New taker dispute'})
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=task, raised_by=self.taker2).exists())
        new_dispute = Dispute.objects.get(task=task)
        self.assertRedirects(response, reverse('dispute_detail', args=[new_dispute.id]))
