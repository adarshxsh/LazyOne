import os
import tempfile
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from basic.models import Task, Dispute, DisputeEvidence, RewardLedger, UserProfile, Notification, Conversation

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
