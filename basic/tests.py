from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeAuditEvent, DisputeEvidence, DisputeVote, Notification
from django.core.management import call_command


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


class DisputeAuditEventAndNotificationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=500)

        self.task = Task.objects.create(
            title="Disputed Task Title",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_audit_event_created_on_dispute_raise(self):
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work claim'}
        )
        dispute = Dispute.objects.get(task=self.task)
        dispute.jurors.add(self.juror1, self.juror2)

        audit_events = DisputeAuditEvent.objects.filter(dispute=dispute)
        self.assertTrue(audit_events.exists())
        event = audit_events.first()
        self.assertEqual(event.event_type, 'dispute_created')
        self.assertEqual(event.actor, self.taker)
        self.assertIn('reason', event.details_json)

        # Check notifications dispatched to poster, taker, and jurors
        dispute.notify_participants("Voting phase started.")
        for user in [self.poster, self.taker, self.juror1, self.juror2]:
            notif = Notification.objects.filter(recipient=user, link=reverse('dispute_detail', args=[dispute.id])).first()
            self.assertIsNotNone(notif, f"Notification missing for user {user.username}")

    def test_audit_event_immutability(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Test dispute",
            deposit_amount=50
        )
        event = DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=self.taker,
            event_type='dispute_created',
            details_json={'reason': 'Test dispute'}
        )

        # Attempt to edit event
        event.event_type = 'modified_event'
        with self.assertRaises(ValueError):
            event.save()

        # Attempt to delete event
        with self.assertRaises(ValueError):
            event.delete()

    def test_evidence_submission_creates_audit_event_and_notifies(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Test dispute",
            deposit_amount=50
        )
        dispute.jurors.add(self.juror1)

        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'evidence': 'Here is proof of completed work screenshot URL.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        evidence = DisputeEvidence.objects.filter(dispute=dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.submitted_by, self.taker)

        audit_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='evidence_submitted').first()
        self.assertIsNotNone(audit_event)
        self.assertEqual(audit_event.actor, self.taker)

        # Verify notifications sent to all participants
        for user in [self.poster, self.taker, self.juror1]:
            notif = Notification.objects.filter(
                recipient=user,
                message__icontains='submitted evidence'
            ).first()
            self.assertIsNotNone(notif)
            self.assertEqual(notif.link, reverse('dispute_detail', args=[dispute.id]))

    def test_vote_casting_creates_audit_event_and_notifies(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Test dispute",
            deposit_amount=50
        )
        dispute.jurors.add(self.juror1)

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('cast_vote', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        vote = DisputeVote.objects.filter(dispute=dispute, voter=self.juror1).first()
        self.assertIsNotNone(vote)
        self.assertEqual(vote.voted_for, self.taker)

        audit_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='vote_cast').first()
        self.assertIsNotNone(audit_event)
        self.assertEqual(audit_event.actor, self.juror1)

        for user in [self.poster, self.taker, self.juror1]:
            notif = Notification.objects.filter(
                recipient=user,
                message__icontains='vote was cast'
            ).first()
            self.assertIsNotNone(notif)

    def test_dispute_withdrawal_creates_resolution_audit_event(self):
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Initial dispute'}
        )
        dispute = Dispute.objects.get(task=self.task)
        dispute.jurors.add(self.juror1)

        # Withdraw
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        resolution_event = DisputeAuditEvent.objects.filter(
            dispute=dispute,
            event_type='dispute_resolved'
        ).first()
        self.assertIsNotNone(resolution_event)
        self.assertEqual(resolution_event.actor, self.taker)

    def test_expired_dispute_auto_resolution_creates_audit_event(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Old dispute",
            deposit_amount=50,
            status='open',
            created_at=timezone.now() - timedelta(days=10)
        )
        dispute.jurors.add(self.juror1)
        # Update created_at in DB directly because auto_now_add overrides on create
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(days=10))

        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        audit_event = DisputeAuditEvent.objects.filter(dispute=dispute, event_type='dispute_resolved').first()
        self.assertIsNotNone(audit_event)
        self.assertIsNone(audit_event.actor)
        self.assertEqual(audit_event.details_json.get('action'), 'auto_resolved_expired')

    def test_dispute_detail_view_renders_chronological_audit_events(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Test dispute",
            deposit_amount=50
        )
        dispute.jurors.add(self.juror1)

        # Log two events
        event1 = DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=self.taker,
            event_type='dispute_created',
            details_json={'reason': 'Initial reason'}
        )
        event2 = DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=self.juror1,
            event_type='vote_cast',
            details_json={'voted_for': 'taker_user'}
        )

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Check chronological order in context
        events = list(response.context['audit_events'])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], event1)
        self.assertEqual(events[1], event2)

        # Check content present in rendered HTML
        self.assertContains(response, 'dispute_created')
        self.assertContains(response, 'vote_cast')


