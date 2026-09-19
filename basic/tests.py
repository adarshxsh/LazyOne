from django.test import TestCase, Client
from django.core.management import call_command
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


class EnforceTaskDeadlinesTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster2', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker2', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

    def test_enforce_deadlines_overdue_available_task(self):
        past_deadline = timezone.now() - timedelta(hours=2)
        task = Task.objects.create(
            title="Overdue Available Task",
            description="Description",
            reward=200,
            posted_by=self.poster,
            status='available',
            deadline=past_deadline
        )

        call_command('enforce_task_deadlines')

        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1200)

        ledger = RewardLedger.objects.filter(
            user=self.poster,
            task=task,
            transaction_type='task_cancellation'
        ).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertEqual(notification.link, reverse('my_tasks'))

    def test_enforce_deadlines_overdue_in_progress_task(self):
        past_deadline = timezone.now() - timedelta(hours=3)
        task = Task.objects.create(
            title="Overdue In Progress Task",
            description="Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=past_deadline
        )

        call_command('enforce_task_deadlines')

        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

        ledger = RewardLedger.objects.filter(
            user=self.poster,
            task=task,
            transaction_type='task_cancellation'
        ).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 300)

        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(poster_notif)
        self.assertEqual(poster_notif.link, reverse('my_tasks'))

        taker_notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(taker_notif)
        self.assertEqual(taker_notif.link, reverse('my_tasks'))

    def test_enforce_deadlines_ignores_future_and_disputed_tasks(self):
        future_deadline = timezone.now() + timedelta(hours=5)
        future_task = Task.objects.create(
            title="Future Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            status='available',
            deadline=future_deadline
        )

        past_deadline = timezone.now() - timedelta(hours=1)
        disputed_task = Task.objects.create(
            title="Disputed Task",
            description="Description",
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=past_deadline
        )

        call_command('enforce_task_deadlines')

        future_task.refresh_from_db()
        self.assertEqual(future_task.status, 'available')

        disputed_task.refresh_from_db()
        self.assertEqual(disputed_task.status, 'disputed')

    def test_home_feed_filters_expired_available_tasks(self):
        future_task = Task.objects.create(
            title="Active Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            status='available',
            deadline=timezone.now() + timedelta(hours=5)
        )
        expired_task = Task.objects.create(
            title="Expired Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            status='available',
            deadline=timezone.now() - timedelta(hours=1)
        )

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        available_tasks = list(response.context['available_tasks'])
        self.assertIn(future_task, available_tasks)
        self.assertNotIn(expired_task, available_tasks)

    def test_take_task_expired_returns_error(self):
        expired_task = Task.objects.create(
            title="Expired Available Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            status='available',
            deadline=timezone.now() - timedelta(hours=1)
        )

        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('take_task', args=[expired_task.id]), follow=True)
        self.assertRedirects(response, reverse('home'))

        expired_task.refresh_from_db()
        self.assertEqual(expired_task.status, 'available')
        self.assertIsNone(expired_task.taken_by)


