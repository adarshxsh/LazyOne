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


class TaskExpirationModelChoicesTests(TestCase):
    def test_task_status_choices_contains_expired(self):
        statuses = dict(Task.STATUS_CHOICES)
        self.assertIn('expired', statuses)
        self.assertEqual(statuses['expired'], 'Expired')

    def test_reward_ledger_transaction_types_contains_task_expiration(self):
        transaction_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('task_expiration', transaction_types)
        self.assertEqual(transaction_types['task_expiration'], 'Task Expiration (Points Refunded)')


class ExpireOverdueTasksWorkerTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster_exp', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, rewards=1000)
        self.taker = User.objects.create_user(username='taker_exp', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, rewards=1000)

    def test_expire_overdue_available_task(self):
        overdue_deadline = timezone.now() - timedelta(hours=2)
        task = Task.objects.create(
            title='Overdue Available Task',
            description='Test task',
            reward=200,
            posted_by=self.poster,
            deadline=overdue_deadline,
            status='available'
        )

        from django.core.management import call_command
        call_command('expire_overdue_tasks')

        task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'expired')
        self.assertEqual(self.poster_profile.rewards, 1200)

        ledger_entry = RewardLedger.objects.filter(task=task, transaction_type='task_expiration').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 200)

        from .models import Notification
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('expired', notification.message)

    def test_expire_overdue_in_progress_task(self):
        overdue_deadline = timezone.now() - timedelta(hours=1)
        task = Task.objects.create(
            title='Overdue In Progress Task',
            description='Test task',
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=overdue_deadline,
            status='in_progress'
        )

        from django.core.management import call_command
        call_command('expire_overdue_tasks')

        task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'expired')
        self.assertEqual(self.poster_profile.rewards, 1300)

        from .models import Notification
        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        taker_notif = Notification.objects.filter(recipient=self.taker).first()

        self.assertIsNotNone(poster_notif)
        self.assertIsNotNone(taker_notif)

    def test_active_future_deadline_task_not_expired(self):
        future_deadline = timezone.now() + timedelta(hours=2)
        task = Task.objects.create(
            title='Future Task',
            description='Future task',
            reward=100,
            posted_by=self.poster,
            deadline=future_deadline,
            status='available'
        )

        from django.core.management import call_command
        call_command('expire_overdue_tasks')

        task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'available')
        self.assertEqual(self.poster_profile.rewards, 1000)

    def test_disputed_task_excluded_from_expiration(self):
        overdue_deadline = timezone.now() - timedelta(hours=5)
        task = Task.objects.create(
            title='Disputed Task',
            description='Disputed',
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=overdue_deadline,
            status='disputed'
        )
        Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Issue with work',
            status='open'
        )

        from django.core.management import call_command
        call_command('expire_overdue_tasks')

        task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'disputed')
        self.assertEqual(self.poster_profile.rewards, 1000)

    def test_command_execution_is_idempotent(self):
        overdue_deadline = timezone.now() - timedelta(hours=2)
        task = Task.objects.create(
            title='Overdue Task Idempotency',
            description='Test',
            reward=250,
            posted_by=self.poster,
            deadline=overdue_deadline,
            status='available'
        )

        from django.core.management import call_command
        call_command('expire_overdue_tasks')
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

        # Second call should not refund again
        call_command('expire_overdue_tasks')
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)
        self.assertEqual(RewardLedger.objects.filter(task=task, transaction_type='task_expiration').count(), 1)


class TakeTaskViewGuardTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_tg', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, rewards=1000)
        self.taker = User.objects.create_user(username='taker_tg', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, rewards=1000)
        self.client.login(username='taker_tg', password='password123')

    def test_take_overdue_task_blocks_and_expires(self):
        overdue_deadline = timezone.now() - timedelta(minutes=30)
        task = Task.objects.create(
            title='Overdue Available Task',
            description='Test description',
            reward=200,
            posted_by=self.poster,
            deadline=overdue_deadline,
            status='available'
        )

        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(task.status, 'expired')
        self.assertIsNone(task.taken_by)
        self.assertEqual(self.poster_profile.rewards, 1200)

    def test_take_valid_task_assigns_taker(self):
        future_deadline = timezone.now() + timedelta(hours=2)
        task = Task.objects.create(
            title='Valid Available Task',
            description='Test description',
            reward=200,
            posted_by=self.poster,
            deadline=future_deadline,
            status='available'
        )

        response = self.client.get(reverse('take_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)


class HomeViewFeedFilterTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_feed', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)

    def test_overdue_and_expired_tasks_filtered_from_home_feed(self):
        now = timezone.now()
        active_task = Task.objects.create(
            title='Active Task',
            description='Valid',
            reward=100,
            posted_by=self.poster,
            deadline=now + timedelta(hours=10),
            status='available'
        )
        overdue_task = Task.objects.create(
            title='Overdue Task',
            description='Past deadline',
            reward=100,
            posted_by=self.poster,
            deadline=now - timedelta(hours=10),
            status='available'
        )
        expired_task = Task.objects.create(
            title='Expired Task',
            description='Already expired',
            reward=100,
            posted_by=self.poster,
            deadline=now - timedelta(hours=10),
            status='expired'
        )

        response = self.client.get(reverse('home'))
        available_tasks = list(response.context['available_tasks'])
        recent_tasks = list(response.context['recent_tasks'])

        self.assertIn(active_task, available_tasks)
        self.assertNotIn(overdue_task, available_tasks)
        self.assertNotIn(expired_task, available_tasks)

        self.assertIn(active_task, recent_tasks)
        self.assertNotIn(overdue_task, recent_tasks)
        self.assertNotIn(expired_task, recent_tasks)

