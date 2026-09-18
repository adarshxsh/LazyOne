from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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


from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification


class DisputeEvidenceAndExpirationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 100})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 500})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client = Client()

    def test_raise_dispute_sets_expiration(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Initial dispute reason'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute
        self.assertIsNotNone(dispute.expires_at)
        expected_min = timezone.now() + timedelta(days=6, hours=23)
        self.assertGreater(dispute.expires_at, expected_min)

    def test_evidence_upload_success_and_notification(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='taker', password='password123')

        dummy_file = SimpleUploadedFile("screenshot.png", b"fake_image_bytes", content_type="image/png")
        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {
            'description': 'Screenshots proving completion',
            'file': dummy_file
        })

        self.assertEqual(response.status_code, 302)
        evidence = DisputeEvidence.objects.filter(dispute=dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.uploaded_by, self.taker)
        self.assertEqual(evidence.description, 'Screenshots proving completion')
        self.assertTrue(evidence.file.name.startswith('dispute_evidence/screenshot'))

        # Check notification sent to poster
        notification = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notification)
        self.assertIn('taker uploaded evidence', notification.message)

    def test_evidence_upload_unauthorized_user(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='other', password='password123')

        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {'description': 'Unauthorized evidence'})
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)

    def test_evidence_upload_invalid_file_format(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='taker', password='password123')

        dummy_file = SimpleUploadedFile("bad_file.exe", b"binary content", content_type="application/octet-stream")
        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {
            'description': 'Executable file',
            'file': dummy_file
        }, follow=True)

        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)
        self.assertContains(response, "Invalid file format")

    def test_evidence_upload_file_size_exceeded(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='taker', password='password123')

        large_content = b"a" * (5 * 1024 * 1024 + 10)
        large_file = SimpleUploadedFile("large_doc.pdf", large_content, content_type="application/pdf")
        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {
            'description': 'Too large',
            'file': large_file
        }, follow=True)

        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)
        self.assertContains(response, "File size exceeds")

    def test_evidence_upload_max_attachment_limit(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason')
        self.client.login(username='taker', password='password123')

        for i in range(5):
            f = SimpleUploadedFile(f"file_{i}.jpg", b"image", content_type="image/jpeg")
            DisputeEvidence.objects.create(dispute=dispute, uploaded_by=self.taker, file=f, description=f"Doc {i}")

        extra_file = SimpleUploadedFile("file_6.jpg", b"image", content_type="image/jpeg")
        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {
            'description': '6th attachment',
            'file': extra_file
        }, follow=True)

        self.assertContains(response, "Maximum limit of 5 evidence file attachments reached")
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 5)

    def test_evidence_upload_resolved_dispute_blocked(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason', status='resolved')
        self.client.login(username='taker', password='password123')

        url = reverse('upload_evidence', args=[dispute.id])
        response = self.client.post(url, {'description': 'Late evidence'}, follow=True)
        self.assertContains(response, "Cannot upload evidence to a resolved")

    def test_management_command_resolves_expired_disputes(self):
        expired_dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Stagnant dispute',
            expires_at=timezone.now() - timedelta(hours=2)
        )
        self.task.status = 'disputed'
        self.task.save()

        other_task = Task.objects.create(
            title='Active Task',
            description='Active Description',
            reward=50,
            posted_by=self.poster,
            taken_by=self.other_user,
            status='disputed'
        )
        active_dispute = Dispute.objects.create(
            task=other_task,
            raised_by=self.other_user,
            reason='Active dispute',
            expires_at=timezone.now() + timedelta(days=5)
        )

        call_command('resolve_expired_disputes')

        expired_dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(expired_dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 200)

        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)

        active_dispute.refresh_from_db()
        other_task.refresh_from_db()
        self.assertEqual(active_dispute.status, 'open')
        self.assertEqual(other_task.status, 'disputed')
