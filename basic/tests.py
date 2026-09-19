import os
import tempfile
import shutil
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.exceptions import ValidationError

from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation, validate_evidence_file
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
        self.client = Client()

        self.poster = User.objects.create_user(username='poster2', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker2', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=1000)

        self.staff_user = User.objects.create_user(username='staff_user', password='password123', is_staff=True)

        self.task = Task.objects.create(
            title="Evidence Test Task",
            description="Testing evidence attachments",
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
            reason="Work submitted but poster refused to approve",
            status='open',
            deposit_amount=50,
            escrow_status='held'
        )

    def test_dispute_creation_default_deadline_and_handler(self):
        self.assertIsNotNone(self.dispute.deadline)
        expected_min = timezone.now() + timedelta(days=2, hours=23)
        self.assertGreater(self.dispute.deadline, expected_min)
        self.assertEqual(self.dispute.expiration_handler, 'refund_poster')

    def test_evidence_file_validation(self):
        valid_png = SimpleUploadedFile("proof.png", b"pngdata", content_type="image/png")
        self.assertIsNone(validate_evidence_file(valid_png))

        valid_pdf = SimpleUploadedFile("doc.pdf", b"pdfdata", content_type="application/pdf")
        self.assertIsNone(validate_evidence_file(valid_pdf))

        invalid_ext = SimpleUploadedFile("script.py", b"python script", content_type="text/plain")
        with self.assertRaises(ValidationError):
            validate_evidence_file(invalid_ext)

        large_file = SimpleUploadedFile("large.txt", b"a" * (5 * 1024 * 1024 + 1), content_type="text/plain")
        with self.assertRaises(ValidationError):
            validate_evidence_file(large_file)

    def test_evidence_form_validation(self):
        empty_form = DisputeEvidenceForm(data={'comment': ''})
        self.assertFalse(empty_form.is_valid())

        comment_form = DisputeEvidenceForm(data={'comment': 'Valid explanation'})
        self.assertTrue(comment_form.is_valid())

        file_upload = SimpleUploadedFile("log.txt", b"some logs", content_type="text/plain")
        file_form = DisputeEvidenceForm(data={'comment': ''}, files={'file': file_upload})
        self.assertTrue(file_form.is_valid())

    def test_dispute_detail_access_control(self):
        # Unauthenticated -> redirect to login
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, f"/login/?next=/dispute/{self.dispute.id}/")

        # Unauthorized user -> redirect to home
        self.client.login(username='other_user', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Authorized poster -> HTTP 200
        self.client.login(username='poster2', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Authorized taker -> HTTP 200
        self.client.login(username='taker2', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Staff user -> HTTP 200
        self.client.login(username='staff_user', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_submit_evidence_view(self):
        self.client.login(username='poster2', password='password123')
        proof_file = SimpleUploadedFile("evidence.png", b"imagedata", content_type="image/png")
        response = self.client.post(
            reverse('dispute_detail', args=[self.dispute.id]),
            {'comment': 'Here is my evidence', 'file': proof_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeEvidence.objects.filter(dispute=self.dispute).count(), 1)
        evidence = DisputeEvidence.objects.first()
        self.assertEqual(evidence.submitted_by, self.poster)
        self.assertEqual(evidence.comment, 'Here is my evidence')
        self.assertTrue(evidence.is_image)
        self.assertEqual(evidence.filename, 'evidence.png')

    def test_check_and_expire_dispute_refund_poster(self):
        initial_rewards = self.poster_profile.rewards
        self.dispute.deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        # Trigger on-demand expiration check
        self.client.login(username='poster2', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_rewards + self.task.reward)
        self.assertEqual(self.dispute.escrow_status, 'refunded')

    def test_check_and_expire_dispute_award_taker(self):
        initial_rewards = self.taker_profile.rewards
        self.dispute.deadline = timezone.now() - timedelta(hours=1)
        self.dispute.expiration_handler = 'award_taker'
        self.dispute.save()

        res = check_and_expire_dispute(self.dispute)
        self.assertTrue(res)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_rewards + self.task.reward + self.dispute.deposit_amount)

    def test_expire_disputes_command(self):
        self.dispute.deadline = timezone.now() - timedelta(hours=2)
        self.dispute.save()

        call_command('expire_disputes')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')


