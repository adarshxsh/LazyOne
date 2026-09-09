from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification
from .workers import process_expired_disputes

class DisputeEvidenceAndExpirationTests(TestCase):

    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123', email='poster@example.com')
        self.taker = User.objects.create_user(username='taker', password='password123', email='taker@example.com')
        self.other_user = User.objects.create_user(username='other', password='password123', email='other@example.com')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Task Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: 'Test Task'"
        )

        self.client = Client()

    def test_raise_dispute_sets_expiration_timestamp(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work was submitted but not marked complete'})
        
        self.assertEqual(response.status_code, 302)
        dispute = Dispute.objects.get(task=self.task)
        self.assertIsNotNone(dispute.expires_at)
        self.assertTrue(dispute.expires_at > timezone.now())
        self.assertEqual(dispute.status, 'open')
        
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_evidence_submission_active_window(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Incomplete work',
            expires_at=timezone.now() + timedelta(days=3)
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        evidence_url = reverse('submit_evidence', args=[dispute.id])
        resp = self.client.post(evidence_url, {
            'text': 'I provided all requirements in the chat link.',
            'attachment_url': 'https://example.com/proof.pdf'
        })
        self.assertEqual(resp.status_code, 302)

        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        evidence = evidences.first()
        self.assertEqual(evidence.submitted_by, self.poster)
        self.assertEqual(evidence.text, 'I provided all requirements in the chat link.')
        self.assertEqual(evidence.attachment_url, 'https://example.com/proof.pdf')

        self.assertTrue(Notification.objects.filter(recipient=self.taker, message__contains='submitted new counter-evidence').exists())

    def test_evidence_submission_file_upload(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute for proof test',
            expires_at=timezone.now() + timedelta(days=2)
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        test_file = SimpleUploadedFile("screenshot.png", b"fake image content", content_type="image/png")

        resp = self.client.post(reverse('submit_evidence', args=[dispute.id]), {
            'text': 'Attached screenshot of delivery',
            'attachment': test_file
        })
        self.assertEqual(resp.status_code, 302)

        ev = DisputeEvidence.objects.get(dispute=dispute)
        self.assertIn('screenshot', ev.attachment.name)

    def test_evidence_submission_validation_failures(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Testing validation',
            expires_at=timezone.now() + timedelta(days=2)
        )
        self.client.login(username='poster', password='password123')

        # Empty text
        resp = self.client.post(reverse('submit_evidence', args=[dispute.id]), {'text': ''})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dispute.evidences.count(), 0)

        # Invalid file format (.exe)
        bad_file = SimpleUploadedFile("malicious.exe", b"executable content", content_type="application/octet-stream")
        resp = self.client.post(reverse('submit_evidence', args=[dispute.id]), {
            'text': 'Try sending exe',
            'attachment': bad_file
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dispute.evidences.count(), 0)

    def test_evidence_submission_blocked_when_expired_or_closed(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Expired dispute test',
            expires_at=timezone.now() - timedelta(hours=1)
        )
        self.client.login(username='poster', password='password123')
        resp = self.client.post(reverse('submit_evidence', args=[dispute.id]), {'text': 'Too late text'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dispute.evidences.count(), 0)

        dispute.expires_at = timezone.now() + timedelta(days=1)
        dispute.status = 'resolved'
        dispute.save()
        resp = self.client.post(reverse('submit_evidence', args=[dispute.id]), {'text': 'Resolved dispute text'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dispute.evidences.count(), 0)

    def test_background_worker_auto_resolves_expired_dispute_no_counter_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker completed work, poster absent',
            expires_at=timezone.now() - timedelta(minutes=10)
        )
        self.task.status = 'disputed'
        self.task.save()

        initial_taker_rewards = self.taker_profile.rewards

        processed = process_expired_disputes()
        self.assertEqual(processed, 1)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 100)

        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

        self.assertTrue(Notification.objects.filter(recipient=self.poster, message__contains='reached expiration deadline').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker, message__contains='reached expiration deadline').exists())

    def test_background_worker_auto_resolves_expired_dispute_with_counter_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work disputed',
            expires_at=timezone.now() - timedelta(minutes=10)
        )
        self.task.status = 'disputed'
        self.task.save()

        DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=self.poster,
            text='I submitted requirements and taker failed to follow.'
        )

        initial_poster_rewards = self.poster_profile.rewards

        processed = process_expired_disputes()
        self.assertEqual(processed, 1)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 100)

        ledger_entry = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_management_command_process_expired_disputes(self):
        from django.core.management import call_command
        from io import StringIO

        Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Expired dispute for command test',
            expires_at=timezone.now() - timedelta(hours=1)
        )
        self.task.status = 'disputed'
        self.task.save()

        out = StringIO()
        call_command('process_expired_disputes', stdout=out)
        self.assertIn('Successfully processed 1 expired dispute(s)', out.getvalue())
