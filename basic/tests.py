from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification

class DisputeEvidenceAndResolutionTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 500})

        self.task = Task.objects.create(
            title="Test Task",
            description="Task description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

    def test_raise_dispute_sets_expiration_and_notifies(self):
        response = self.client_taker.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not fair'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute
        self.assertIsNotNone(dispute.expires_at)
        self.assertAlmostEqual(dispute.expires_at, dispute.created_at + timedelta(days=7), delta=timedelta(seconds=5))

        noti = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(noti)
        self.assertIn("raised a dispute", noti.message)

    def test_multi_party_evidence_upload(self):
        self.client_taker.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = self.task.dispute

        test_file = SimpleUploadedFile("proof.png", b"file_content", content_type="image/png")
        resp = self.client_poster.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'Here is proof I provided instructions', 'file': test_file}
        )
        self.assertEqual(dispute.evidence.count(), 1)
        ev = dispute.evidence.first()
        self.assertEqual(ev.submitted_by, self.poster)
        self.assertEqual(ev.statement, 'Here is proof I provided instructions')
        self.assertTrue(ev.file.name.endswith('proof.png'))

        noti = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(noti)
        self.assertIn("submitted counter-evidence", noti.message)

        resp_taker = self.client_taker.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'My counter argument'}
        )
        self.assertEqual(dispute.evidence.count(), 2)

    def test_invalid_file_extension_rejected(self):
        self.client_taker.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = self.task.dispute

        bad_file = SimpleUploadedFile("script.exe", b"binary", content_type="application/x-msdownload")
        resp = self.client_poster.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'Testing bad file', 'file': bad_file}
        )
        self.assertEqual(dispute.evidence.count(), 0)

    def test_oversized_file_rejected(self):
        self.client_taker.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = self.task.dispute

        large_content = b"0" * (5 * 1024 * 1024 + 100)
        large_file = SimpleUploadedFile("huge.pdf", large_content, content_type="application/pdf")
        resp = self.client_poster.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'Too big', 'file': large_file}
        )
        self.assertEqual(dispute.evidence.count(), 0)

    def test_non_participant_cannot_submit_evidence(self):
        self.client_taker.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = self.task.dispute

        resp = self.client_other.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'Unauthorized evidence'}
        )
        self.assertEqual(dispute.evidence.count(), 0)

    def test_auto_resolution_management_command(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Abandoned by taker',
            expires_at=timezone.now() - timedelta(minutes=5)
        )
        self.task.status = 'disputed'
        self.task.save()

        initial_poster_rewards = self.poster_profile.rewards

        call_command('resolve_expired_disputes')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(task=self.task, user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

        noti = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(noti)
        self.assertIn("expired and was automatically resolved", noti.message)

    def test_prevent_mutation_on_resolved_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Done dispute',
            status='resolved',
            expires_at=timezone.now() - timedelta(days=1)
        )
        self.task.status = 'disputed'
        self.task.save()

        resp = self.client_poster.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'statement': 'Late evidence'}
        )
        self.assertEqual(dispute.evidence.count(), 0)

        resp = self.client_poster.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
