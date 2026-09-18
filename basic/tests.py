import os
import tempfile
import shutil
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.core.management import call_command
from django.core.exceptions import ValidationError

from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification, Conversation, validate_evidence_file
from .forms import DisputeEvidenceForm
from .views.dispute import check_and_expire_dispute


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


TEMP_MEDIA_DIR = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_DIR)
class DisputeEvidenceAndExpirationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_DIR, ignore_errors=True)
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.task = Task.objects.create(
            title="Fix bug in code",
            description="Need help fixing a bug",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but poster refusing to pay",
            status='open'
        )

        self.client = Client()

    def test_dispute_creation_default_deadline_and_handler(self):
        """Test Dispute model sets deadline automatically and defaults expiration_handler."""
        self.assertIsNotNone(self.dispute.deadline)
        expected_deadline_min = timezone.now() + timedelta(days=2, hours=23)
        self.assertGreater(self.dispute.deadline, expected_deadline_min)
        self.assertEqual(self.dispute.expiration_handler, 'refund_poster')

    def test_evidence_file_validation_allowed_and_disallowed_extensions(self):
        """Test validation of file extension and size limits."""
        valid_file = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        self.assertIsNone(validate_evidence_file(valid_file))

        valid_pdf = SimpleUploadedFile("doc.pdf", b"pdf_content", content_type="application/pdf")
        self.assertIsNone(validate_evidence_file(valid_pdf))

        invalid_ext_file = SimpleUploadedFile("script.py", b"print('hello')", content_type="text/plain")
        with self.assertRaises(ValidationError):
            validate_evidence_file(invalid_ext_file)

        large_content = b"a" * (5 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile("large.txt", large_content, content_type="text/plain")
        with self.assertRaises(ValidationError):
            validate_evidence_file(large_file)

    def test_evidence_model_properties(self):
        """Test DisputeEvidence model properties and file storage."""
        img_file = SimpleUploadedFile("proof.jpg", b"image_data", content_type="image/jpeg")
        evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            submitted_by=self.taker,
            comment="Here is the screenshot proof",
            file=img_file
        )
        self.assertTrue(evidence.is_image)
        self.assertTrue(evidence.filename.startswith("proof"))
        self.assertTrue(evidence.filename.endswith(".jpg"))
        self.assertTrue(evidence.file.name.startswith("dispute_evidence/"))

    def test_evidence_form_validation(self):
        """Test DisputeEvidenceForm requires at least comment or file."""
        form_empty = DisputeEvidenceForm(data={'comment': ''})
        self.assertFalse(form_empty.is_valid())

        form_comment = DisputeEvidenceForm(data={'comment': 'Valid comment'})
        self.assertTrue(form_comment.is_valid())

        f = SimpleUploadedFile("note.txt", b"some text", content_type="text/plain")
        form_file = DisputeEvidenceForm(data={'comment': ''}, files={'file': f})
        self.assertTrue(form_file.is_valid())

    def test_dispute_detail_access_control(self):
        """Test that only poster, taker, and staff can view dispute details."""
        # Unauthenticated user redirected to login
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, f"/login/?next=/dispute/{self.dispute.id}/")

        # Unrelated user redirected to home
        self.client.login(username='other', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Poster can access
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Taker can access
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Staff can access
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_submit_evidence_by_poster_and_taker(self):
        """Test that poster and taker can submit evidence on dispute_detail view."""
        self.client.login(username='poster', password='password123')
        proof_txt = SimpleUploadedFile("logs.txt", b"log output", content_type="text/plain")
        response = self.client.post(
            reverse('dispute_detail', args=[self.dispute.id]),
            {'comment': 'I never received the completed code', 'file': proof_txt},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 1)
        evidence = DisputeEvidence.objects.first()
        self.assertEqual(evidence.submitted_by, self.poster)
        self.assertEqual(evidence.comment, 'I never received the completed code')

        # Check notification sent to counterparty (taker)
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

        # Now taker submits evidence
        self.client.login(username='taker', password='password123')
        proof_img = SimpleUploadedFile("screen.png", b"pngdata", content_type="image/png")
        response = self.client.post(
            reverse('dispute_detail', args=[self.dispute.id]),
            {'comment': 'Here is the screenshot of sent file', 'file': proof_img},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 2)

    def test_on_demand_dispute_expiration_check(self):
        """Test check_and_expire_dispute auto-resolves expired disputes and refunds escrowed points."""
        initial_poster_rewards = self.poster_profile.rewards

        # Set dispute deadline in the past
        self.dispute.deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        # Accessing dispute detail view triggers on-demand expiration check
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        # Verify RewardLedger transaction created
        ledger = RewardLedger.objects.filter(task=self.task, user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

        # Verify notifications created
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_award_taker_expiration_handler(self):
        """Test expiration when handler is set to award_taker."""
        initial_taker_rewards = self.taker_profile.rewards

        self.dispute.deadline = timezone.now() - timedelta(hours=1)
        self.dispute.expiration_handler = 'award_taker'
        self.dispute.save()

        check_and_expire_dispute(self.dispute)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(task=self.task, user=self.taker, transaction_type='dispute_payout').first()
        self.assertIsNotNone(ledger)

    def test_expire_disputes_management_command(self):
        """Test python manage.py expire_disputes command."""
        initial_poster_rewards = self.poster_profile.rewards

        # Set dispute deadline in the past
        self.dispute.deadline = timezone.now() - timedelta(hours=2)
        self.dispute.save()

        # Create a second dispute that is NOT expired
        task2 = Task.objects.create(
            title="Task 2", description="Desc 2", reward=100,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute2 = Dispute.objects.create(
            task=task2, raised_by=self.taker, reason="Reason 2",
            status='open', deadline=timezone.now() + timedelta(hours=10)
        )

        call_command('expire_disputes')

        self.dispute.refresh_from_db()
        dispute2.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(dispute2.status, 'open')

    def test_withdrawal_prevents_expiration(self):
        """Test withdrawing a dispute before deadline sets dispute to resolved."""
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
