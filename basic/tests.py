import tempfile
from django.test import TestCase, Client, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
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


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DisputeEvidenceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.unrelated_user = User.objects.create_user(username='otheruser', password='password123')
        UserProfile.objects.create(user=self.unrelated_user, rewards=1000)

        self.task = Task.objects.create(
            title="Task with Evidence",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but unpaid",
            deposit_amount=50,
            escrow_status='held'
        )

    def test_model_creation_and_properties(self):
        file_data = SimpleUploadedFile("proof.png", b"file_content_here", content_type="image/png")
        evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploaded_by=self.taker,
            file=file_data,
            description="Screenshot of completed work"
        )
        self.assertTrue(evidence.filename.startswith("proof"))
        self.assertTrue(evidence.filename.endswith(".png"))
        self.assertTrue(evidence.is_image)
        self.assertIn("Evidence for Dispute", str(evidence))

    def test_upload_evidence_success(self):
        self.client.login(username='taker', password='password123')
        png_file = SimpleUploadedFile("screenshot.png", b"fake_png_binary_data", content_type="image/png")

        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'file': png_file, 'description': 'Work completion proof'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 1)

        evidence = DisputeEvidence.objects.get(dispute=self.dispute)
        self.assertEqual(evidence.uploaded_by, self.taker)
        self.assertEqual(evidence.description, 'Work completion proof')
        self.assertIn('screenshot', evidence.filename.lower())
        self.assertContains(response, 'Evidence uploaded successfully.')
        self.assertContains(response, 'screenshot')

    def test_upload_evidence_disallowed_extension(self):
        self.client.login(username='taker', password='password123')
        exe_file = SimpleUploadedFile("script.exe", b"binary_data", content_type="application/octet-stream")

        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'file': exe_file, 'description': 'Executable file'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)
        self.assertContains(response, "is not allowed")

    def test_upload_evidence_file_size_limit(self):
        self.client.login(username='taker', password='password123')
        large_content = b"x" * (10 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile("large_doc.pdf", large_content, content_type="application/pdf")

        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'file': large_file, 'description': 'Too large'},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)
        self.assertContains(response, "exceeds maximum limit")

    def test_upload_evidence_unauthorized_user(self):
        self.client.login(username='otheruser', password='password123')
        txt_file = SimpleUploadedFile("notes.txt", b"some text", content_type="text/plain")

        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'file': txt_file, 'description': 'Unauthorized upload'},
            follow=True
        )
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)
        self.assertContains(response, "not authorized")


