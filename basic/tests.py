from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
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


class DisputeEvidenceTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title="Dispute Evidence Test Task",
            description="Testing evidence uploads",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_raise_dispute_with_evidence_files(self):
        self.client.login(username='taker', password='password123')
        file1 = SimpleUploadedFile("proof1.png", b"file_content_png", content_type="image/png")
        file2 = SimpleUploadedFile("proof2.pdf", b"file_content_pdf", content_type="application/pdf")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work submitted but poster refused to complete.',
                'evidence_files': [file1, file2],
                'description': 'Initial dispute proof attachments'
            }
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        evidences = DisputeEvidence.objects.filter(dispute=dispute)
        self.assertEqual(evidences.count(), 2)
        filenames = [e.filename for e in evidences]
        self.assertTrue(any('proof1' in name for name in filenames))
        self.assertTrue(any('proof2' in name for name in filenames))
        for e in evidences:
            self.assertEqual(e.uploaded_by, self.taker)
            self.assertEqual(e.description, 'Initial dispute proof attachments')

    def test_upload_evidence_on_active_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Incomplete work claim',
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        # Taker uploads evidence
        self.client.login(username='taker', password='password123')
        file_taker = SimpleUploadedFile("taker_log.txt", b"log data", content_type="text/plain")
        res1 = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [file_taker], 'description': 'Taker execution log'}
        )
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))

        # Poster uploads counter-evidence
        self.client.login(username='poster', password='password123')
        file_poster = SimpleUploadedFile("poster_screenshot.jpg", b"image data", content_type="image/jpeg")
        res2 = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [file_poster], 'description': 'Poster counter evidence screenshot'}
        )
        self.assertRedirects(res2, reverse('dispute_detail', args=[dispute.id]))

        evidences = DisputeEvidence.objects.filter(dispute=dispute).order_by('uploaded_at')
        self.assertEqual(evidences.count(), 2)
        self.assertEqual(evidences[0].uploaded_by, self.taker)
        self.assertEqual(evidences[1].uploaded_by, self.poster)

        # View detail page
        detail_res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(detail_res.status_code, 200)
        self.assertContains(detail_res, 'taker_log')
        self.assertContains(detail_res, 'poster_screenshot')

    def test_upload_evidence_invalid_extension_rejected(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            status='open'
        )
        self.client.login(username='taker', password='password123')
        invalid_file = SimpleUploadedFile("script.sh", b"echo hello", content_type="text/x-sh")

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [invalid_file]}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)

    def test_upload_evidence_exceeds_size_limit(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            status='open'
        )
        self.client.login(username='taker', password='password123')
        # 10 MB + 10 bytes file
        large_content = b"a" * (10 * 1024 * 1024 + 10)
        large_file = SimpleUploadedFile("big_file.pdf", large_content, content_type="application/pdf")

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [large_file]}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)

    def test_upload_evidence_unauthorized_user(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            status='open'
        )
        self.client.login(username='other_user', password='password123')
        valid_file = SimpleUploadedFile("evidence.png", b"data", content_type="image/png")

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [valid_file]}
        )
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)

    def test_upload_evidence_closed_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            status='resolved'
        )
        self.client.login(username='taker', password='password123')
        valid_file = SimpleUploadedFile("evidence.png", b"data", content_type="image/png")

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [valid_file]}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)


