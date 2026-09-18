import os
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
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
class ExpireTasksTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})

    def test_expire_available_task_past_deadline(self):
        past_deadline = timezone.now() - timedelta(hours=2)
        task = Task.objects.create(
            title="Overdue Available Task",
            description="Fix the tap",
            reward=200,
            posted_by=self.poster,
            deadline=past_deadline,
            status='available'
        )
        self.poster_profile.rewards -= 200
        self.poster_profile.save()

        call_command('expire_tasks')

        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        ledger_entry = RewardLedger.objects.filter(
            user=self.poster,
            task=task,
            transaction_type='task_cancellation'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 200)
        self.assertEqual(ledger_entry.description, f"Refund for expired task: '{task.title}'")

        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn("Overdue Available Task", notification.message)
        self.assertIn("expired", notification.message)

    def test_expire_in_progress_task_past_deadline(self):
        past_deadline = timezone.now() - timedelta(hours=1)
        task = Task.objects.create(
            title="Overdue In Progress Task",
            description="Clean garage",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=past_deadline,
            status='in_progress'
        )
        self.poster_profile.rewards -= 300
        self.poster_profile.save()

        call_command('expire_tasks')

        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(poster_notif)
        self.assertIn("expired", poster_notif.message)

        taker_notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(taker_notif)
        self.assertIn("expired", taker_notif.message)

    def test_disputed_task_not_expired(self):
        past_deadline = timezone.now() - timedelta(hours=3)
        task = Task.objects.create(
            title="Disputed Task",
            description="Paint fence",
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=past_deadline,
            status='disputed'
        )

        call_command('expire_tasks')

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')

        self.assertEqual(
            RewardLedger.objects.filter(task=task, transaction_type='task_cancellation').count(),
            0
        )

    def test_future_deadline_task_not_expired(self):
        future_deadline = timezone.now() + timedelta(days=1)
        task = Task.objects.create(
            title="Future Task",
            description="Mow lawn",
            reward=100,
            posted_by=self.poster,
            deadline=future_deadline,
            status='available'
        )

        call_command('expire_tasks')

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')

    def test_take_task_past_deadline_rejected(self):
        past_deadline = timezone.now() - timedelta(hours=1)
        task = Task.objects.create(
            title="Expired Available Task",
            description="Write essay",
            reward=100,
            posted_by=self.poster,
            deadline=past_deadline,
            status='available'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', kwargs={'task_id': task.id}), follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

    def test_cron_endpoint_authentication_and_execution(self):
        past_deadline = timezone.now() - timedelta(hours=2)
        task = Task.objects.create(
            title="Cron Test Task",
            description="Test endpoint",
            reward=250,
            posted_by=self.poster,
            deadline=past_deadline,
            status='available'
        )
        self.poster_profile.rewards -= 250
        self.poster_profile.save()

        cron_url = reverse('process_expired_tasks')

        # Test unauthorized request (no token)
        response = self.client.get(cron_url)
        self.assertEqual(response.status_code, 401)

        # Test unauthorized request (invalid token)
        response = self.client.get(cron_url, {'token': 'wrong-secret'})
        self.assertEqual(response.status_code, 401)

        # Test authorized request with token in GET param
        secret = os.getenv('CRON_SECRET') or 'secret'
        response = self.client.get(f"{cron_url}?token={secret}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertGreaterEqual(data['expired_tasks_count'], 1)

        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
