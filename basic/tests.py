from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from django.db import DatabaseError, transaction
from .models import UserProfile, Task, Dispute, DisputeAuditEvent, Notification, RewardLedger, Conversation
from .signals import dispute_state_changed


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


class DisputeSignalAuditNotificationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_audit', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_audit', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Audit Test Task",
            description="Testing Audit Signals",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_audit_event_immutability(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Testing immutability",
            deposit_amount=50,
            escrow_status='held'
        )
        event = DisputeAuditEvent.objects.filter(dispute=dispute).first()
        self.assertIsNotNone(event)

        # Updating immutable audit event must raise ValueError
        with self.assertRaises(ValueError):
            event.new_status = 'modified'
            event.save()

        # Deleting immutable audit event must raise ValueError
        with self.assertRaises(ValueError):
            event.delete()

    def test_raise_dispute_creates_audit_event_and_notifies_both_parties(self):
        self.client.login(username='taker_audit', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        audit_events = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='RAISED')
        self.assertTrue(audit_events.exists())
        event = audit_events.first()
        self.assertEqual(event.actor, self.taker)

        # Both poster and taker receive notifications
        poster_notifs = Notification.objects.filter(recipient=self.poster)
        taker_notifs = Notification.objects.filter(recipient=self.taker)
        self.assertTrue(poster_notifs.exists())
        self.assertTrue(taker_notifs.exists())

        detail_url = reverse('dispute_detail', args=[dispute.id])
        self.assertEqual(poster_notifs.first().link, detail_url)
        self.assertEqual(taker_notifs.first().link, detail_url)

    def test_withdraw_dispute_creates_audit_event_and_notifies_both_parties(self):
        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'To be withdrawn'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Clear notifications before withdraw
        Notification.objects.all().delete()

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

        audit_events = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='WITHDRAWN')
        self.assertTrue(audit_events.exists())
        event = audit_events.first()
        self.assertEqual(event.actor, self.taker)

        # Both poster and taker receive notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_complete_disputed_task_creates_audit_event_and_notifies_dispute_raiser(self):
        # Taker raises dispute
        self.client.login(username='taker_audit', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster not responding'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Clear notifications
        Notification.objects.all().delete()

        # Poster completes task
        self.client.login(username='poster_audit', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        audit_events = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='RESOLVED')
        self.assertTrue(audit_events.exists())
        event = audit_events.first()
        self.assertEqual(event.actor, self.poster)

        # Dispute raiser (taker) and poster both notified
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_resolve_expired_disputes_command_creates_audit_events_and_notifications(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Old dispute",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        # Backdate creation
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(days=10))
        dispute.refresh_from_db()

        Notification.objects.all().delete()

        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        expired_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='EXPIRED').first()
        self.assertIsNotNone(expired_event)
        self.assertIsNone(expired_event.actor)

        # Both participants notified
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_unchanged_dispute_save_ignores_audit_logging(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Initial reason",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        initial_event_count = DisputeAuditEvent.objects.filter(dispute=dispute).count()
        initial_notif_count = Notification.objects.count()

        # Unchanged save
        dispute.reason = "Initial reason updated description without status change"
        dispute.save()

        self.assertEqual(DisputeAuditEvent.objects.filter(dispute=dispute).count(), initial_event_count)
        self.assertEqual(Notification.objects.count(), initial_notif_count)

    def test_transaction_rollback_prevents_audit_event_and_notifications(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Rollback test",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        initial_event_count = DisputeAuditEvent.objects.count()
        initial_notif_count = Notification.objects.count()

        try:
            with transaction.atomic():
                dispute.status = 'resolved'
                dispute.save()
                raise DatabaseError("Forced rollback")
        except DatabaseError:
            pass

        self.assertEqual(DisputeAuditEvent.objects.count(), initial_event_count)
        self.assertEqual(Notification.objects.count(), initial_notif_count)


