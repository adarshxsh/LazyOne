from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification
from basic.views.dispute import process_expired_disputes

class DisputeEvidenceAndTimeoutTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.stranger = User.objects.create_user(username='stranger', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)
        self.stranger_profile = UserProfile.objects.create(user=self.stranger, rewards=500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.doer,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_doer = Client()
        self.client_doer.login(username='doer', password='password123')

        self.client_stranger = Client()
        self.client_stranger.login(username='stranger', password='password123')

    def test_raise_dispute_sets_deadlines(self):
        response = self.client_doer.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work submitted but poster refused to complete.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        dispute = self.task.dispute
        self.assertEqual(dispute.status, 'open')
        self.assertIsNotNone(dispute.evidence_deadline)
        self.assertIsNotNone(dispute.expires_at)
        self.assertTrue(dispute.evidence_deadline > dispute.created_at)
        self.assertTrue(dispute.expires_at > dispute.evidence_deadline)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_dispute_detail_view_permissions(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Initial dispute reason'
        )
        # Poster and Doer can access
        resp_poster = self.client_poster.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp_poster.status_code, 200)

        resp_doer = self.client_doer.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp_doer.status_code, 200)

        # Stranger cannot access
        resp_stranger = self.client_stranger.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(resp_stranger, reverse('home'))

    def test_submit_valid_evidence_text_and_file(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Initial dispute reason'
        )

        dummy_image = SimpleUploadedFile(
            "proof.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
            content_type="image/png"
        )

        response = self.client_doer.post(
            reverse('submit_evidence', args=[dispute.id]),
            {
                'description': 'Here is screenshot proof of work done.',
                'file': dummy_image
            }
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'under_review')

        evidence = dispute.evidence_entries.first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.submitted_by, self.doer)
        self.assertIn('screenshot proof', evidence.description)
        self.assertTrue(evidence.file.name.endswith('proof.png'))

        # Check notification dispatched to poster
        notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notif)
        self.assertIn('doer uploaded new evidence', notif.message)

    def test_invalid_evidence_file_extension_rejected(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Initial dispute reason'
        )

        invalid_file = SimpleUploadedFile(
            "script.exe",
            b"binary content",
            content_type="application/octet-stream"
        )

        response = self.client_doer.post(
            reverse('submit_evidence', args=[dispute.id]),
            {
                'description': 'Submitting invalid file.',
                'file': invalid_file
            }
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_oversized_evidence_file_rejected(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Initial dispute reason'
        )

        large_content = b"a" * (10 * 1024 * 1024 + 100)  # slightly over 10 MB
        large_file = SimpleUploadedFile(
            "large_doc.pdf",
            large_content,
            content_type="application/pdf"
        )

        response = self.client_doer.post(
            reverse('submit_evidence', args=[dispute.id]),
            {
                'description': 'Submitting oversized file.',
                'file': large_file
            }
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_evidence_submission_closed_after_deadline(self):
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Expired dispute',
            evidence_deadline=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=24)
        )

        response = self.client_doer.post(
            reverse('submit_evidence', args=[dispute.id]),
            {'description': 'Late submission.'}
        )

        # After processing expired dispute or checking deadline, request fails
        dispute.refresh_from_db()
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_auto_resolution_when_doer_provided_evidence(self):
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Doer raised dispute and provided proof',
            evidence_deadline=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1)
        )

        DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=self.doer,
            description='I finished the task cleanly.'
        )

        processed = process_expired_disputes()
        self.assertEqual(processed, 1)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'expired')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.doer_profile.rewards, 700)  # 500 + 200 reward

        ledger = RewardLedger.objects.filter(user=self.doer, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Both notified
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.doer).count(), 1)

    def test_auto_resolution_refund_poster_when_no_doer_evidence(self):
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Doer raised dispute but provided no evidence',
            evidence_deadline=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1)
        )

        processed = process_expired_disputes()
        self.assertEqual(processed, 1)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'expired')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200)  # 1000 + 200 refund

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

    def test_process_expired_disputes_management_command(self):
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Expired dispute for command test',
            evidence_deadline=now - timedelta(hours=5),
            expires_at=now - timedelta(hours=2)
        )

        call_command('process_expired_disputes')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'expired')
