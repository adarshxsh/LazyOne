import os
import tempfile
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeEvidence


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


TEMP_MEDIA_DIR = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_DIR)
class DisputeEvidenceAndExpirationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.worker = User.objects.create_user(username='worker', password='password123')
        self.stranger = User.objects.create_user(username='stranger', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.worker_profile, _ = UserProfile.objects.get_or_create(user=self.worker, defaults={'rewards': 1000})
        self.stranger_profile, _ = UserProfile.objects.get_or_create(user=self.stranger, defaults={'rewards': 1000})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.worker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.worker)

        self.client = Client()

    def test_dispute_creation_defaults_expires_at(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Worker raised dispute'
        )
        self.assertIsNotNone(dispute.expires_at)
        self.assertTrue(dispute.expires_at > timezone.now() + timedelta(days=6))
        self.assertEqual(dispute.status, 'open')

    def test_raise_dispute_with_file_attachment(self):
        self.client.login(username='worker', password='password123')
        png_file = SimpleUploadedFile("evidence.png", b"file_content", content_type="image/png")

        response = self.client.post(
            f'/task/dispute/{self.task.id}/',
            {'reason': 'Work delivered accurately', 'attachment': png_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = self.task.dispute
        self.assertEqual(dispute.reason, 'Work delivered accurately')

        evidence = dispute.evidence.first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.evidence_type, 'file')
        self.assertEqual(evidence.user, self.worker)
        self.assertTrue(evidence.attachment.name.startswith('dispute_evidence/'))

    def test_invalid_file_extension_and_size_rejected(self):
        self.client.login(username='worker', password='password123')

        # Invalid extension
        exe_file = SimpleUploadedFile("malware.exe", b"binary", content_type="application/octet-stream")
        response = self.client.post(
            f'/task/dispute/{self.task.id}/',
            {'reason': 'Dispute with exe', 'attachment': exe_file},
            follow=True
        )
        self.assertIn("Unsupported file format", response.content.decode())
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # File size > 10MB
        large_file = SimpleUploadedFile("large.pdf", b"0" * (10 * 1024 * 1024 + 100), content_type="application/pdf")
        response = self.client.post(
            f'/task/dispute/{self.task.id}/',
            {'reason': 'Dispute with large file', 'attachment': large_file},
            follow=True
        )
        self.assertIn("File size cannot exceed 10 MB", response.content.decode())

    def test_submit_dispute_evidence_authorization(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Initial dispute'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Stranger attempts to submit evidence -> denied
        self.client.login(username='stranger', password='password123')
        response = self.client.post(
            f'/dispute/{dispute.id}/evidence/',
            {'description': 'Stranger evidence'},
            follow=True
        )
        self.assertIn("not authorized", response.content.decode().lower())
        self.assertEqual(dispute.evidence.count(), 0)

        # Poster submits counter-evidence -> success
        self.client.login(username='poster', password='password123')
        pdf_file = SimpleUploadedFile("proof.pdf", b"proof_content", content_type="application/pdf")
        response = self.client.post(
            f'/dispute/{dispute.id}/evidence/',
            {'description': 'Poster counter claim', 'attachment': pdf_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispute.evidence.count(), 1)
        evidence = dispute.evidence.first()
        self.assertEqual(evidence.user, self.poster)
        self.assertEqual(evidence.description, 'Poster counter claim')

    def test_dispute_detail_rendering_and_countdown(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Initial reason'
        )
        DisputeEvidence.objects.create(
            dispute=dispute,
            user=self.worker,
            evidence_type='text',
            description='Evidence note'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(f'/dispute/{dispute.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Evidence Timeline')
        self.assertContains(response, 'Evidence note')
        self.assertContains(response, 'expiration-countdown')

    def test_process_expired_disputes_fallback_poster_refund(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Initial dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Set expiration in the past
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        initial_poster_rewards = self.poster_profile.rewards

        call_command('process_expired_disputes')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').exists())

    def test_process_expired_disputes_fallback_worker_award(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Initial dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Worker submits evidence, poster submits no evidence
        DisputeEvidence.objects.create(
            dispute=dispute,
            user=self.worker,
            evidence_type='text',
            description='Worker submitted proof of work'
        )

        # Set expiration in the past
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        initial_worker_rewards = self.worker_profile.rewards

        call_command('process_expired_disputes')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.worker_profile.rewards, initial_worker_rewards + self.task.reward)
        self.assertTrue(RewardLedger.objects.filter(user=self.worker, task=self.task, transaction_type='task_completion').exists())
