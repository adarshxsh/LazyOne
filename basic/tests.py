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


from .models import DisputeAuditEvent, Notification
from django.core.management import call_command
from django.db import transaction, DatabaseError


class DisputeAuditLoggingAndSignalTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.reviewer = User.objects.create_user(username='reviewer', password='password123')
        self.reviewer_profile = UserProfile.objects.create(user=self.reviewer, rewards=500)

        self.task = Task.objects.create(
            title="Audit Test Task",
            description="Testing Audit Logging",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=3)
        )
        self.conv = Conversation.objects.create(task=self.task)
        self.conv.participants.add(self.poster, self.taker, self.reviewer)

    def test_raise_dispute_creates_audit_event_and_notifications(self):
        self.client.login(username='taker', password='password123')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('raise_dispute', args=[self.task.id]),
                {'reason': 'Incomplete instructions'}
            )
        dispute = Dispute.objects.get(task=self.task)
        audit_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='DISPUTE_RAISED').first()
        self.assertIsNotNone(audit_event)
        self.assertEqual(audit_event.actor, self.taker)
        self.assertEqual(audit_event.metadata.get('reason'), 'Incomplete instructions')

        # Multi-party notifications should be dispatched to poster, taker, and reviewer (conversation participant)
        poster_notifs = Notification.objects.filter(recipient=self.poster)
        taker_notifs = Notification.objects.filter(recipient=self.taker)
        reviewer_notifs = Notification.objects.filter(recipient=self.reviewer)

        self.assertTrue(poster_notifs.exists())
        self.assertTrue(taker_notifs.exists())
        self.assertTrue(reviewer_notifs.exists())

    def test_add_evidence_creates_audit_event_and_notifications(self):
        self.client.login(username='taker', password='password123')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('raise_dispute', args=[self.task.id]),
                {'reason': 'Need clarify'}
            )
        dispute = Dispute.objects.get(task=self.task)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('add_evidence', args=[dispute.id]),
                {'evidence': 'Attached screenshot of finished work.'}
            )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        evidence_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='EVIDENCE_ADDED').first()
        self.assertIsNotNone(evidence_event)
        self.assertEqual(evidence_event.actor, self.taker)
        self.assertEqual(evidence_event.metadata.get('evidence'), 'Attached screenshot of finished work.')

    def test_withdraw_dispute_creates_audit_event(self):
        self.client.login(username='taker', password='password123')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('raise_dispute', args=[self.task.id]),
                {'reason': 'Mistake'}
            )
        dispute = Dispute.objects.get(task=self.task)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        withdraw_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='DISPUTE_WITHDRAWN').first()
        self.assertIsNotNone(withdraw_event)
        self.assertEqual(withdraw_event.actor, self.taker)

    def test_resolve_dispute_via_complete_task_creates_audit_event(self):
        self.client.login(username='taker', password='password123')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('raise_dispute', args=[self.task.id]),
                {'reason': 'Pending work'}
            )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.get(reverse('complete_task', args=[self.task.id]))

        resolve_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='DISPUTE_RESOLVED').first()
        self.assertIsNotNone(resolve_event)
        self.assertEqual(resolve_event.actor, self.poster)

    def test_auto_expiration_creates_expired_audit_event(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Expired dispute test',
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        # Set created_at to 10 days ago
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        with self.captureOnCommitCallbacks(execute=True):
            call_command('resolve_expired_disputes', days=7)

        expired_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='DISPUTE_EXPIRED').first()
        self.assertIsNotNone(expired_event)
        self.assertIsNone(expired_event.actor)
        self.assertEqual(expired_event.metadata.get('days_sla'), 7)

    def test_transaction_rollback_prevents_audit_and_notification(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Rollback test',
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        initial_event_count = DisputeAuditEvent.objects.count()
        initial_notif_count = Notification.objects.count()

        try:
            with transaction.atomic():
                from .signals import dispute_state_changed
                dispute_state_changed.send(
                    sender=Dispute,
                    dispute=dispute,
                    event_type='DISPUTE_RESOLVED',
                    actor=self.poster,
                    metadata={'test': 'rollback'}
                )
                # Force a rollback
                raise DatabaseError("Forced rollback")
        except DatabaseError:
            pass

        # Since transaction was rolled back, on_commit should not fire
        self.assertEqual(DisputeAuditEvent.objects.count(), initial_event_count)
        self.assertEqual(Notification.objects.count(), initial_notif_count)

