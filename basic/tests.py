from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification

class AutomatedDisputeSettlementTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )

    def test_raise_dispute_with_file_attachment_and_expiration(self):
        self.client.login(username='taker', password='password123')
        evidence_file = SimpleUploadedFile("evidence.txt", b"Proof of work done.", content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster refused to accept completion.', 'evidence_files': evidence_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = self.task.dispute
        self.assertIsNotNone(dispute)
        self.assertEqual(dispute.status, 'open')
        self.assertIsNotNone(dispute.expires_at)
        self.assertGreater(dispute.expires_at, timezone.now())

        # Check evidence attached
        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        self.assertTrue(evidences.first().file.name.startswith('dispute_evidence/'))

    def test_file_size_exceeds_10mb_limit(self):
        self.client.login(username='taker', password='password123')
        # Create dummy file > 10MB
        large_content = b"x" * (10 * 1024 * 1024 + 10)
        large_file = SimpleUploadedFile("large.txt", large_content, content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Too big file.', 'evidence_files': large_file},
            follow=True
        )
        self.assertContains(response, "File attachments must not exceed 10 MB per upload.")
        self.assertIsNone(self.task.dispute)

    def test_submit_counter_evidence(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Poster submits counter-evidence
        self.client.login(username='poster', password='password123')
        counter_file = SimpleUploadedFile("counter.txt", b"Counter proof.", content_type="text/plain")
        response = self.client.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'content': 'I disagree with taker.', 'evidence_files': counter_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        self.assertEqual(evidences.first().sender, self.poster)
        self.assertEqual(evidences.first().content, 'I disagree with taker.')

        # Verify notification sent to taker
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_withdraw_dispute_soft_deletes_record(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Confirm record was NOT hard-deleted from database
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_auto_resolve_expired_dispute_taker_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Expired dispute test'}, follow=True)
        dispute = self.task.dispute

        # Set expiration date to the past
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Trigger background webhook
        response = self.client.get(reverse('auto_resolve_disputes'))
        self.assertEqual(response.status_code, 200)
        json_resp = response.json()
        self.assertEqual(json_resp['status'], 'success')
        self.assertEqual(json_resp['processed_count'], 1)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'auto_resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check taker received reward
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700) # 500 + 200

        # Check ledger audit entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_settlement'
        ).first()
        self.assertIsNotNone(ledger_entry)

        # Check notifications sent to both users
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_auto_resolve_expired_dispute_poster_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Expired dispute test'}, follow=True)
        dispute = self.task.dispute

        # Poster responds with evidence
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('submit_dispute_evidence', args=[dispute.id]), {'content': 'Poster counter-evidence'}, follow=True)

        # Set expiration date to the past (taker failed to respond after poster's evidence)
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Trigger background webhook
        response = self.client.get(reverse('auto_resolve_disputes'))
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'auto_resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Check poster refunded reward
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 + 200

        # Check ledger audit entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='dispute_settlement'
        ).first()
        self.assertIsNotNone(ledger_entry)

    def test_manual_task_completion_overrides_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Poster manually completes task during active dispute
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('complete_task', args=[self.task.id]), follow=True)
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700) # 500 + 200
