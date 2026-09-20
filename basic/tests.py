from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation
from .forms import DisputeEvidenceForm, validate_evidence_file
from django.core.exceptions import ValidationError


class DisputeEvidenceTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=self.other_user, rewards=500)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Evidence Test Task",
            description="Testing dispute evidence attachments",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_model_and_properties(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Incomplete deliverables",
            deposit_amount=50
        )
        dummy_file = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=dummy_file,
            description="Proof of completion"
        )

        self.assertEqual(evidence.user, self.taker)
        self.assertTrue(evidence.filename.startswith("screenshot"))
        self.assertIn("Evidence", str(evidence))
        self.assertEqual(dispute.evidences.count(), 1)

    def test_form_file_extension_validation(self):
        # Invalid extension .exe
        bad_file = SimpleUploadedFile("malicious.exe", b"binary_content", content_type="application/x-msdownload")
        form = DisputeEvidenceForm(files={'file': bad_file}, data={'description': 'Test'})
        self.assertFalse(form.is_valid())
        self.assertIn('file', form.errors)

        # Valid extension .png
        good_file = SimpleUploadedFile("proof.png", b"image_data", content_type="image/png")
        form = DisputeEvidenceForm(files={'file': good_file}, data={'description': 'Valid proof'})
        self.assertTrue(form.is_valid())

    def test_form_file_size_validation(self):
        # File larger than 10MB
        large_content = b"x" * (10 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile("big.pdf", large_content, content_type="application/pdf")
        form = DisputeEvidenceForm(files={'file': large_file})
        self.assertFalse(form.is_valid())
        self.assertIn('file', form.errors)

    def test_raise_dispute_with_evidence(self):
        self.client.login(username='taker', password='password123')
        evidence_file = SimpleUploadedFile("output.txt", b"Task output log content", content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster not responding', 'evidence': evidence_file, 'description': 'Log output'},
            format='multipart'
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.evidences.count(), 1)

        evidence = dispute.evidences.first()
        self.assertEqual(evidence.uploaded_by, self.taker)
        self.assertEqual(evidence.description, 'Log output')

    def test_upload_dispute_evidence_poster_and_worker(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Issue with completion",
            deposit_amount=50
        )
        self.task.status = 'disputed'
        self.task.save()

        # Worker uploads evidence
        self.client.login(username='taker', password='password123')
        f1 = SimpleUploadedFile("worker_proof.jpg", b"worker_data", content_type="image/jpeg")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': f1, 'description': 'Worker screenshot'},
            format='multipart'
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.evidences.count(), 1)

        # Poster uploads counter-evidence
        self.client.login(username='poster', password='password123')
        f2 = SimpleUploadedFile("poster_spec.pdf", b"pdf_data", content_type="application/pdf")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': f2, 'description': 'Requirements document'},
            format='multipart'
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.evidences.count(), 2)

    def test_unauthorized_user_cannot_upload_evidence_or_view(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Issue",
            deposit_amount=50
        )
        self.client.login(username='other', password='password123')

        # View detail
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Upload evidence
        f = SimpleUploadedFile("other.png", b"data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': f},
            format='multipart'
        )
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(dispute.evidences.count(), 0)

    def test_cannot_upload_evidence_for_resolved_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Resolved dispute",
            deposit_amount=50,
            status='resolved'
        )
        self.client.login(username='taker', password='password123')

        f = SimpleUploadedFile("test.png", b"data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': f},
            format='multipart'
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.evidences.count(), 0)

    def test_dispute_detail_view_renders_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Evidence detail test",
            deposit_amount=50
        )
        f = SimpleUploadedFile("sample_doc.pdf", b"pdf content", content_type="application/pdf")
        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=f,
            description="Sample PDF Evidence"
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sample_doc")
        self.assertContains(response, "Sample PDF Evidence")
        self.assertContains(response, "Download Attachment")
        self.assertContains(response, evidence.file.url)


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

