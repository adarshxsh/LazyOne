from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation, Notification
from .forms import DisputeEvidenceForm


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


class DisputeEvidenceModelAndFormTests(TestCase):
    def setUp(self):
        self.user_poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.create(user=self.user_poster, rewards=1000)
        self.user_taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.user_taker, rewards=1500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Task Description',
            reward=100,
            posted_by=self.user_poster,
            taken_by=self.user_taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.user_taker,
            reason='Poster did not confirm task completion.'
        )

    def test_form_valid_image_upload(self):
        image = SimpleUploadedFile("screenshot.jpg", b"fake_image_bytes", content_type="image/jpeg")
        form = DisputeEvidenceForm(data={}, files={'file': image})
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_valid_pdf_upload(self):
        pdf = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 fake_pdf_bytes", content_type="application/pdf")
        form = DisputeEvidenceForm(data={'description': 'PDF receipt'}, files={'file': pdf})
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_valid_url_only(self):
        form = DisputeEvidenceForm(data={'url': 'https://example.com/proof'})
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_invalid_empty(self):
        form = DisputeEvidenceForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("Please provide at least a file, URL, or description", str(form.errors))

    def test_form_invalid_file_format(self):
        exe = SimpleUploadedFile("malware.exe", b"binary_data", content_type="application/octet-stream")
        form = DisputeEvidenceForm(data={}, files={'file': exe})
        self.assertFalse(form.is_valid())
        self.assertIn("Invalid file format", str(form.errors))

    def test_form_invalid_file_size_exceeds_10mb(self):
        oversized_data = b"x" * (10 * 1024 * 1024 + 100)
        big_file = SimpleUploadedFile("large_image.png", oversized_data, content_type="image/png")
        form = DisputeEvidenceForm(data={}, files={'file': big_file})
        self.assertFalse(form.is_valid())
        self.assertIn("File size must be under 10MB", str(form.errors))

    def test_evidence_model_properties(self):
        image_file = SimpleUploadedFile("photo.png", b"img_data", content_type="image/png")
        evidence_img = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploaded_by=self.user_taker,
            file=image_file,
            description="Screenshot"
        )
        self.assertTrue(evidence_img.is_image)
        self.assertFalse(evidence_img.is_pdf)

        pdf_file = SimpleUploadedFile("doc.pdf", b"%PDF-1.4 doc", content_type="application/pdf")
        evidence_pdf = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploaded_by=self.user_poster,
            file=pdf_file,
            description="Doc"
        )
        self.assertFalse(evidence_pdf.is_image)
        self.assertTrue(evidence_pdf.is_pdf)


class DisputeEvidenceViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user_poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.create(user=self.user_poster, rewards=1000)

        self.user_taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.user_taker, rewards=1500)

        self.user_other = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=self.user_other, rewards=500)

        self.task = Task.objects.create(
            title='Sample Task',
            description='Sample Task Description',
            reward=200,
            posted_by=self.user_poster,
            taken_by=self.user_taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.user_taker,
            reason='Delivery dispute'
        )

    def test_upload_evidence_success(self):
        self.client.login(username='taker', password='password123')
        url = reverse('upload_dispute_evidence', args=[self.dispute.id])
        data = {
            'url': 'https://example.com/delivery-proof',
            'description': 'Submitted delivery proof link'
        }
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 1)
        evidence = DisputeEvidence.objects.first()
        self.assertEqual(evidence.uploaded_by, self.user_taker)
        self.assertEqual(evidence.url, 'https://example.com/delivery-proof')

    def test_upload_evidence_unauthorized_user(self):
        self.client.login(username='other', password='password123')
        url = reverse('upload_dispute_evidence', args=[self.dispute.id])
        data = {'description': 'Unauthorized evidence'}
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)

    def test_upload_evidence_on_resolved_dispute_fails(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        url = reverse('upload_dispute_evidence', args=[self.dispute.id])
        data = {'description': 'Late evidence'}
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)

    def test_upload_evidence_on_expired_dispute_fails(self):
        self.dispute.status = 'expired'
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        url = reverse('upload_dispute_evidence', args=[self.dispute.id])
        data = {'description': 'Late evidence after expiration'}
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 0)


class ExpireStaleDisputesCommandTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster_exp', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_exp', password='password123')
        UserProfile.objects.create(user=self.taker, rewards=1500)

    def test_expire_stale_dispute_unresponsive_poster(self):
        # Taker raised dispute 8 days ago, poster never responded
        task = Task.objects.create(
            title='Stale Task Taker Favored',
            description='Test',
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Poster ignored completed work'
        )
        # Backdate dispute creation to 8 days ago
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(days=8))

        call_command('expire_stale_disputes', days=7)

        dispute.refresh_from_db()
        task.refresh_from_db()
        taker_profile = UserProfile.objects.get(user=self.taker)

        self.assertEqual(dispute.status, 'expired')
        self.assertEqual(task.status, 'completed')
        self.assertEqual(taker_profile.rewards, 1800)  # 1500 + 300
        self.assertTrue(
            RewardLedger.objects.filter(user=self.taker, task=task, transaction_type='task_completion').exists()
        )
        self.assertTrue(
            Notification.objects.filter(recipient=self.taker).exists()
        )

    def test_expire_stale_dispute_unresponsive_taker(self):
        # Poster raised dispute 8 days ago, taker never responded
        task = Task.objects.create(
            title='Stale Task Poster Favored',
            description='Test',
            reward=250,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.poster,
            reason='Taker abandoned work'
        )
        # Backdate dispute creation to 8 days ago
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(days=8))

        call_command('expire_stale_disputes', days=7)

        dispute.refresh_from_db()
        task.refresh_from_db()
        poster_profile = UserProfile.objects.get(user=self.poster)

        self.assertEqual(dispute.status, 'expired')
        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(poster_profile.rewards, 1250)  # 1000 + 250
        self.assertTrue(
            RewardLedger.objects.filter(user=self.poster, task=task, transaction_type='task_cancellation').exists()
        )

    def test_recent_dispute_does_not_expire(self):
        # Dispute raised 3 days ago should remain open
        task = Task.objects.create(
            title='Recent Task',
            description='Test',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker,
            reason='Recent issue'
        )
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(days=3))

        call_command('expire_stale_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')
