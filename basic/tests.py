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


class DisputeAuditAndLifecycleServiceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_audit', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_audit', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.task = Task.objects.create(
            title="Audit Task",
            description="Task for testing dispute audit lifecycle",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=3)
        )
        Conversation.objects.create(task=self.task)

    def test_raise_dispute_creates_audit_event_and_notifications(self):
        self.client.login(username='taker_audit', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        audit_events = dispute.audit_events.all()
        self.assertEqual(audit_events.count(), 1)
        event = audit_events.first()
        self.assertEqual(event.event_type, 'raised')
        self.assertEqual(event.actor, self.taker)
        self.assertEqual(event.state_after, 'open')
        self.assertIn('deposit_amount', event.metadata)

        # Multi-party notification checks for participants
        self.assertTrue(self.poster.notifications.exists())
        self.assertTrue(self.taker.notifications.exists())

    def test_withdraw_dispute_creates_audit_event(self):
        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        audit_events = dispute.audit_events.all().order_by('timestamp')
        self.assertEqual(audit_events.count(), 2)

        withdraw_event = audit_events.last()
        self.assertEqual(withdraw_event.event_type, 'withdrawn')
        self.assertEqual(withdraw_event.actor, self.taker)
        self.assertEqual(withdraw_event.state_before, 'open')
        self.assertEqual(withdraw_event.state_after, 'resolved')

    def test_task_completion_creates_resolution_audit_event(self):
        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster_audit', password='password123')
        self.client.get(reverse('complete_task', args=[self.task.id]))

        audit_events = dispute.audit_events.all().order_by('timestamp')
        self.assertEqual(audit_events.count(), 2)

        completion_event = audit_events.last()
        self.assertEqual(completion_event.event_type, 'resolved_on_task_completion')
        self.assertEqual(completion_event.actor, self.poster)
        self.assertEqual(completion_event.state_after, 'resolved')

    def test_sla_command_creates_system_audit_event(self):
        from basic.models import DisputeAuditEvent
        from django.core.management import call_command

        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        audit_events = dispute.audit_events.all().order_by('timestamp')
        self.assertEqual(audit_events.count(), 2)

        sla_event = audit_events.last()
        self.assertEqual(sla_event.event_type, 'sla_auto_resolved')
        self.assertIsNone(sla_event.actor)
        self.assertTrue(sla_event.metadata.get('system'))

    def test_audit_event_immutability(self):
        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )
        dispute = Dispute.objects.get(task=self.task)
        event = dispute.audit_events.first()

        # Update attempt must raise ValueError
        event.event_type = 'tampered'
        with self.assertRaises(ValueError):
            event.save()

        # Delete attempt must raise ValueError
        with self.assertRaises(ValueError):
            event.delete()


