import os
import tempfile
import shutil
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from .models import UserProfile, Task, Dispute, DisputeAttachment, RewardLedger, Conversation


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


class DisputeAttachmentTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.mkdtemp()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.temp_media,
            PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
        )
        self.settings_override.enable()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='otheruser', password='password123')
        self.staff_user = User.objects.create_user(username='staffuser', password='password123', is_staff=True)

        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.other_user)
        UserProfile.objects.get_or_create(user=self.staff_user)

        self.task = Task.objects.create(
            title='Test Task for Evidence',
            description='Deliver website mockup',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.temp_media, ignore_errors=True)

    def test_model_properties_and_signal_deletion(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Deliverable not accepted'
        )
        test_file = SimpleUploadedFile("screenshot.png", b"fake_png_data", content_type="image/png")
        attachment = DisputeAttachment.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=test_file,
            file_name="screenshot.png",
            file_size=len(b"fake_png_data")
        )

        self.assertTrue(attachment.is_image)
        self.assertEqual(attachment.formatted_file_size, "13 B")
        file_path = attachment.file.path
        self.assertTrue(os.path.exists(file_path))

        attachment.delete()
        self.assertFalse(os.path.exists(file_path))

    def test_file_size_formatting(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        att_b = DisputeAttachment(file_size=500)
        self.assertEqual(att_b.formatted_file_size, "500 B")

        att_kb = DisputeAttachment(file_size=2048)
        self.assertEqual(att_kb.formatted_file_size, "2.0 KB")

        att_mb = DisputeAttachment(file_size=5242880)
        self.assertEqual(att_mb.formatted_file_size, "5.0 MB")

    def test_raise_dispute_with_valid_attachment(self):
        self.client.login(username='taker', password='password123')
        uploaded_png = SimpleUploadedFile("proof.png", b"image bytes", content_type="image/png")
        uploaded_txt = SimpleUploadedFile("notes.txt", b"some notes text", content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work completed but poster refuses payment.',
                'attachments': [uploaded_png, uploaded_txt]
            },
            follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.attachments.count(), 2)

        filenames = [att.file_name for att in dispute.attachments.all()]
        self.assertIn("proof.png", filenames)
        self.assertIn("notes.txt", filenames)

    def test_raise_dispute_disallowed_extension(self):
        self.client.login(username='taker', password='password123')
        bad_file = SimpleUploadedFile("malicious.exe", b"binary content", content_type="application/octet-stream")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work completed.',
                'attachments': [bad_file]
            },
            follow=True
        )

        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

    def test_upload_attachment_post_creation_by_participants(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker raised dispute')
        self.task.status = 'disputed'
        self.task.save()

        # Task Poster uploads evidence
        self.client.login(username='poster', password='password123')
        counter_proof = SimpleUploadedFile("contract.pdf", b"%PDF-1.4 dummy pdf", content_type="application/pdf")
        response = self.client.post(
            reverse('upload_dispute_attachment', args=[dispute.id]),
            {'attachments': counter_proof},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispute.attachments.count(), 1)
        att = dispute.attachments.first()
        self.assertEqual(att.uploaded_by, self.poster)
        self.assertEqual(att.file_name, "contract.pdf")

        # Task Taker uploads additional evidence
        self.client.login(username='taker', password='password123')
        log_file = SimpleUploadedFile("activity.log", b"system log entries", content_type="text/plain")
        response = self.client.post(
            reverse('upload_dispute_attachment', args=[dispute.id]),
            {'attachments': log_file},
            follow=True
        )
        self.assertEqual(dispute.attachments.count(), 2)

    def test_upload_attachment_unauthorized_user_blocked(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker raised dispute')
        self.client.login(username='otheruser', password='password123')

        evidence = SimpleUploadedFile("unauthorized.png", b"data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_attachment', args=[dispute.id]),
            {'attachments': evidence},
            follow=True
        )
        self.assertEqual(dispute.attachments.count(), 0)

    def test_download_attachment_permissions(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker raised dispute')
        attachment = DisputeAttachment.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=SimpleUploadedFile("deliverable.zip", b"zip data contents", content_type="application/zip"),
            file_name="deliverable.zip",
            file_size=len(b"zip data contents")
        )

        # Poster download -> allowed
        self.client.login(username='poster', password='password123')
        resp = self.client.get(reverse('download_dispute_attachment', args=[attachment.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['Content-Disposition'], 'attachment; filename="deliverable.zip"')

        # Staff download -> allowed
        self.client.login(username='staffuser', password='password123')
        resp = self.client.get(reverse('download_dispute_attachment', args=[attachment.id]))
        self.assertEqual(resp.status_code, 200)

        # Unauthorized user -> blocked
        self.client.login(username='otheruser', password='password123')
        resp = self.client.get(reverse('download_dispute_attachment', args=[attachment.id]), follow=True)
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

    def test_dispute_detail_rendering(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute details rendering test')
        attachment = DisputeAttachment.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=SimpleUploadedFile("preview.png", b"png_data", content_type="image/png"),
            file_name="preview.png",
            file_size=8
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "preview.png")
        self.assertContains(response, "Upload Evidence")
        self.assertContains(response, reverse('download_dispute_attachment', args=[attachment.id]))

    def test_file_size_exceeded_rejected(self):
        self.client.login(username='taker', password='password123')
        large_file = SimpleUploadedFile("big.png", b"x" * (10 * 1024 * 1024 + 1), content_type="image/png")
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Reason', 'attachments': [large_file]},
            follow=True
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_upload_to_resolved_dispute_rejected(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason', status='resolved')
        self.client.login(username='poster', password='password123')
        evidence = SimpleUploadedFile("counter.png", b"data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_attachment', args=[dispute.id]),
            {'attachments': evidence},
            follow=True
        )
        self.assertEqual(dispute.attachments.count(), 0)

    def test_upload_without_files_rejected(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('upload_dispute_attachment', args=[dispute.id]),
            {},
            follow=True
        )
        self.assertEqual(dispute.attachments.count(), 0)
