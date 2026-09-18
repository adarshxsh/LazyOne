import os
import tempfile
import shutil
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation


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


# Create temporary directory for media files during tests
TEMP_MEDIA_ROOT = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class DisputeEvidenceTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.executor = User.objects.create_user(username='executor', password='password123')
        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.admin_user = User.objects.create_superuser(username='admin', password='password123', is_staff=True)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Task Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.executor,
            status='disputed'
        )

        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.executor)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.executor,
            reason='Task completion issue',
            status='open'
        )

    def tearDown(self):
        if os.path.exists(TEMP_MEDIA_ROOT):
            shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

    def test_dispute_evidence_model_creation(self):
        dummy_file = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploader=self.executor,
            file=dummy_file,
            description="Proof of completion"
        )
        self.assertEqual(evidence.dispute, self.dispute)
        self.assertEqual(evidence.uploader, self.executor)
        self.assertEqual(evidence.description, "Proof of completion")
        self.assertIn(f"disputes/dispute_{self.dispute.id}/", evidence.file.name)
        self.assertEqual(list(self.dispute.evidence_items.all()), [evidence])
        self.assertEqual(list(self.dispute.evidence.all()), [evidence])

    def test_poster_and_executor_can_upload_evidence(self):
        # Test executor upload
        self.client.login(username='executor', password='password123')
        file1 = SimpleUploadedFile("proof1.png", b"image data 1", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Executor evidence'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute, uploader=self.executor).count(), 1)

        # Test poster upload
        self.client.login(username='poster', password='password123')
        file2 = SimpleUploadedFile("doc2.pdf", b"pdf data 2", content_type="application/pdf")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file2, 'description': 'Poster counter-evidence'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute, uploader=self.poster).count(), 1)

    def test_multi_file_upload(self):
        self.client.login(username='executor', password='password123')
        file1 = SimpleUploadedFile("proof1.png", b"image data 1", content_type="image/png")
        file2 = SimpleUploadedFile("proof2.jpg", b"image data 2", content_type="image/jpeg")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': [file1, file2], 'description': 'Multiple proof files'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 2)

    def test_non_participant_cannot_upload_or_view(self):
        self.client.login(username='other_user', password='password123')
        # View dispute detail
        view_resp = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(view_resp, reverse('home'))

        # Upload evidence
        file1 = SimpleUploadedFile("proof.png", b"data", content_type="image/png")
        upload_resp = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Unauthorized upload'}
        )
        self.assertRedirects(upload_resp, reverse('home'))
        self.assertEqual(DisputeEvidence.objects.count(), 0)

    def test_upload_disabled_when_dispute_resolved(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='executor', password='password123')
        file1 = SimpleUploadedFile("proof.png", b"data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Resolved dispute upload'},
            follow=True
        )
        self.assertEqual(DisputeEvidence.objects.count(), 0)
        self.assertContains(response, "Cannot upload evidence for a resolved dispute.")

    def test_file_size_validation(self):
        self.client.login(username='executor', password='password123')
        # 11 MB file
        large_content = b"x" * (11 * 1024 * 1024)
        file1 = SimpleUploadedFile("large_file.zip", large_content, content_type="application/zip")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Too large file'},
            follow=True
        )
        self.assertEqual(DisputeEvidence.objects.count(), 0)
        self.assertContains(response, "exceeds the maximum allowed size of 10 MB")

    def test_file_extension_validation(self):
        self.client.login(username='executor', password='password123')
        file1 = SimpleUploadedFile("script.exe", b"binary content", content_type="application/octet-stream")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Executable file'},
            follow=True
        )
        self.assertEqual(DisputeEvidence.objects.count(), 0)
        self.assertContains(response, "is not allowed")

    def test_storage_directory_isolation(self):
        self.client.login(username='executor', password='password123')
        file1 = SimpleUploadedFile("test_attachment.png", b"attachment data", content_type="image/png")
        self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {'file': file1, 'description': 'Test attachment'},
            follow=True
        )
        evidence = DisputeEvidence.objects.get(dispute=self.dispute)
        expected_subdir = f"disputes/dispute_{self.dispute.id}/"
        self.assertIn(expected_subdir, evidence.file.name)
        full_path = os.path.join(settings.MEDIA_ROOT, evidence.file.name)
        self.assertTrue(os.path.exists(full_path))

    def test_dispute_detail_rendering(self):
        evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploader=self.executor,
            file=SimpleUploadedFile("render_test.png", b"render_data", content_type="image/png"),
            description="Rendered description test"
        )
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Submitted by: executor")
        self.assertContains(response, "Rendered description test")
        self.assertContains(response, evidence.file.url)
        self.assertContains(response, "Download File")

