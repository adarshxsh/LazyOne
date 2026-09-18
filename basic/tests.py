from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.exceptions import ValidationError

from basic.models import (
    UserProfile, Task, Dispute, DisputeEvidence,
    RewardLedger, Notification, Conversation, validate_evidence_file
)
from basic.views.dispute import process_dispute_expiration


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


class DisputeEvidenceTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=2),
            status='disputed'
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker dispute reason'
        )

        self.client = Client()

    def test_dispute_default_fields(self):
        self.assertFalse(self.dispute.is_expired)
        self.assertIsNone(self.dispute.resolution_reason)
        self.assertGreater(self.dispute.evidence_deadline, timezone.now() + timedelta(days=6))

    def test_file_validation_valid(self):
        txt_file = SimpleUploadedFile("evidence.txt", b"Hello world text evidence", content_type="text/plain")
        try:
            validate_evidence_file(txt_file)
        except ValidationError:
            self.fail("validate_evidence_file raised ValidationError unexpectedly for valid txt file!")

        pdf_file = SimpleUploadedFile("doc.pdf", b"%PDF-1.4...", content_type="application/pdf")
        try:
            validate_evidence_file(pdf_file)
        except ValidationError:
            self.fail("validate_evidence_file raised ValidationError unexpectedly for valid pdf file!")

    def test_file_validation_invalid_extension(self):
        exe_file = SimpleUploadedFile("malware.exe", b"MZ...", content_type="application/x-msdownload")
        with self.assertRaises(ValidationError):
            validate_evidence_file(exe_file)

    def test_file_validation_oversized(self):
        large_content = b"0" * (10 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile("big.txt", large_content, content_type="text/plain")
        with self.assertRaises(ValidationError):
            validate_evidence_file(large_file)

    def test_submit_evidence_by_participants(self):
        self.client.login(username='taker', password='password123')
        url = reverse('submit_dispute_evidence', args=[self.dispute.id])

        response = self.client.post(url, {
            'title': 'Taker Evidence Title',
            'description': 'Description of work completed',
            'evidence_type': 'text'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.assertEqual(DisputeEvidence.objects.count(), 1)
        ev = DisputeEvidence.objects.first()
        self.assertEqual(ev.title, 'Taker Evidence Title')
        self.assertEqual(ev.user, self.taker)
        self.assertEqual(ev.submitted_by, self.taker)

        # Check notification sent to poster
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_submit_evidence_unauthorized_user(self):
        self.client.login(username='other', password='password123')
        url = reverse('submit_dispute_evidence', args=[self.dispute.id])

        response = self.client.post(url, {
            'title': 'Unauthorized Evidence',
            'description': 'Attempting unauthorized submission',
            'evidence_type': 'text'
        })
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.assertEqual(DisputeEvidence.objects.count(), 0)

    def test_submit_evidence_rejected_after_deadline(self):
        # Set deadline to the past
        self.dispute.evidence_deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        url = reverse('submit_dispute_evidence', args=[self.dispute.id])

        response = self.client.post(url, {
            'title': 'Late Evidence',
            'description': 'Too late',
            'evidence_type': 'text'
        })
        self.assertEqual(DisputeEvidence.objects.count(), 0)

    def test_dispute_expiration_favors_taker_when_only_taker_submitted(self):
        # Taker submits evidence
        DisputeEvidence.objects.create(
            dispute=self.dispute,
            user=self.taker,
            title='Proof from Taker',
            description='Work was done',
            evidence_type='text'
        )

        # Set deadline to past
        self.dispute.evidence_deadline = timezone.now() - timedelta(minutes=1)
        self.dispute.save()

        # Run process_dispute_expiration
        resolved = process_dispute_expiration(self.dispute)
        self.assertTrue(resolved)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertTrue(self.dispute.is_expired)
        self.assertIn("taker", self.dispute.resolution_reason.lower())
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 600)  # 500 + 100 reward

        # Check ledger entry
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

    def test_dispute_expiration_favors_poster_when_only_poster_submitted(self):
        # Poster submits evidence
        DisputeEvidence.objects.create(
            dispute=self.dispute,
            user=self.poster,
            title='Proof from Poster',
            description='Work was NOT done',
            evidence_type='text'
        )

        # Set deadline to past
        self.dispute.evidence_deadline = timezone.now() - timedelta(minutes=1)
        self.dispute.save()

        # Run process_dispute_expiration
        resolved = process_dispute_expiration(self.dispute)
        self.assertTrue(resolved)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertTrue(self.dispute.is_expired)
        self.assertIn("poster", self.dispute.resolution_reason.lower())
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1100)  # 1000 + 100 refund

        # Check ledger entry
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

    def test_expire_disputes_management_command(self):
        # Set deadline to past
        self.dispute.evidence_deadline = timezone.now() - timedelta(minutes=10)
        self.dispute.save()

        call_command('expire_disputes')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertTrue(self.dispute.is_expired)
