from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
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

    def test_raise_dispute_populates_voting_deadline(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'SLA test reason'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertIsNotNone(dispute.voting_deadline)
        self.assertEqual(dispute.voting_period_days, 7)
        expected_min = timezone.now() + timedelta(days=6, hours=23)
        expected_max = timezone.now() + timedelta(days=7, hours=1)
        self.assertTrue(expected_min <= dispute.voting_deadline <= expected_max)

    def test_dispute_detail_template_renders_voting_deadline(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'SLA test reason'}
        )
        dispute = Dispute.objects.get(task=self.task)
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voting Deadline:")
        self.assertContains(response, "Expiration Status:")

    def test_resolve_expired_disputes_command_sweeps_past_deadlines(self):
        now = timezone.now()
        # Expired dispute raised by taker
        expired_dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unresponsive poster",
            deposit_amount=60,
            escrow_status='held',
            status='open',
            voting_period_days=7,
            voting_deadline=now - timedelta(days=1)
        )
        self.task.status = 'disputed'
        self.task.save()

        # Active dispute raised on small_task
        active_dispute = Dispute.objects.create(
            task=self.small_task,
            raised_by=self.taker,
            reason="Active dispute",
            deposit_amount=50,
            escrow_status='held',
            status='open',
            voting_period_days=7,
            voting_deadline=now + timedelta(days=5)
        )
        self.small_task.status = 'disputed'
        self.small_task.save()

        # Execute sweep command
        call_command('resolve_expired_disputes')

        expired_dispute.refresh_from_db()
        self.assertEqual(expired_dispute.status, 'resolved')
        self.assertEqual(expired_dispute.escrow_status, 'refunded')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker profile: initial 100 + task reward 300 + deposit refund 60 = 460
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 460)

        # Active dispute should remain open
        active_dispute.refresh_from_db()
        self.assertEqual(active_dispute.status, 'open')
        self.assertEqual(active_dispute.escrow_status, 'held')

        # Notifications should be dispatched
        notifications = Notification.objects.filter(recipient=self.taker)
        self.assertTrue(notifications.exists())

    def test_resolve_expired_disputes_command_poster_raised(self):
        now = timezone.now()
        expired_dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason="Unresponsive taker",
            deposit_amount=60,
            escrow_status='held',
            status='open',
            voting_period_days=7,
            voting_deadline=now - timedelta(hours=2)
        )
        self.task.status = 'disputed'
        self.task.save()

        call_command('resolve_expired_disputes')

        expired_dispute.refresh_from_db()
        self.assertEqual(expired_dispute.status, 'resolved')
        self.assertEqual(expired_dispute.escrow_status, 'refunded')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Poster profile: initial 1000 + task reward refund 300 + deposit refund 60 = 1360
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1360)

