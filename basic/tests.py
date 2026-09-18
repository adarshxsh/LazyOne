import os
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation, Notification, dispute_evidence_upload_to


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


class DisputeEvidenceModelTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task deliverable not accepted'
        )

    def test_upload_to_path(self):
        evidence = DisputeEvidence(dispute=self.dispute)
        path = dispute_evidence_upload_to(evidence, 'sample_screenshot.PNG')
        self.assertTrue(path.startswith(f"disputes/{self.dispute.id}/evidence/"))
        self.assertTrue(path.endswith('.png'))

    def test_is_image_property(self):
        img_file = SimpleUploadedFile('test.jpg', b'fake image', content_type='image/jpeg')
        doc_file = SimpleUploadedFile('test.pdf', b'fake pdf', content_type='application/pdf')

        img_evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            submitted_by=self.taker,
            file=img_file,
            title='Image Test',
            mime_type='image/jpeg',
            evidence_type='screenshot'
        )
        doc_evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            submitted_by=self.poster,
            file=doc_file,
            title='Doc Test',
            mime_type='application/pdf',
            evidence_type='document'
        )

        self.assertTrue(img_evidence.is_image)
        self.assertFalse(doc_evidence.is_image)
        self.assertIn("Evidence for Dispute", str(img_evidence))


class DisputeViewsTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)
        self.unauthorized_user = User.objects.create_user(username='stranger', password='password123')
        self.staff_user = User.objects.create_user(username='admin', password='password123', is_staff=True)

        self.task = Task.objects.create(
            title='Build Website',
            description='Need a Django website',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.client = Client()

    def test_raise_dispute_with_initial_evidence(self):
        self.client.login(username='taker', password='password123')

        img_file = SimpleUploadedFile('proof.png', b'image data', content_type='image/png')
        doc_file = SimpleUploadedFile('log.txt', b'chat log data', content_type='text/plain')

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work completed but poster refuses to release reward.',
                'evidence_files': [img_file, doc_file],
                'evidence_type': 'deliverable_proof',
                'title': 'Deliverable Screenshots',
                'description': 'Initial work proof'
            },
            follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = self.task.dispute
        self.assertEqual(dispute.reason, 'Work completed but poster refuses to release reward.')
        self.assertEqual(dispute.evidence.count(), 2)

        ev1 = dispute.evidence.first()
        self.assertEqual(ev1.submitted_by, self.taker)
        self.assertEqual(ev1.evidence_type, 'deliverable_proof')

    def test_add_dispute_evidence_by_counterparty(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Initial dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Poster submits rebuttal evidence
        self.client.login(username='poster', password='password123')
        rebuttal_file = SimpleUploadedFile('rebuttal.pdf', b'%PDF-1.4 test', content_type='application/pdf')

        response = self.client.post(
            reverse('add_dispute_evidence', args=[dispute.id]),
            {
                'evidence_files': [rebuttal_file],
                'evidence_type': 'document',
                'title': 'Original Spec Document',
                'description': 'Task requirement specifications'
            },
            follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispute.evidence.count(), 1)
        evidence = dispute.evidence.first()
        self.assertEqual(evidence.submitted_by, self.poster)
        self.assertEqual(evidence.evidence_type, 'document')
        self.assertEqual(evidence.title, 'Original Spec Document')

        # Check counterparty notification
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn("uploaded new evidence", notification.message)

    def test_permissions_access_control(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Unauthorized user viewing dispute detail -> 403 Forbidden
        self.client.login(username='stranger', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 403)

        # Unauthorized user posting evidence -> 403 Forbidden
        sample_file = SimpleUploadedFile('test.png', b'png data', content_type='image/png')
        response = self.client.post(
            reverse('add_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [sample_file]}
        )
        self.assertEqual(response.status_code, 403)

        # Poster, Taker, and Staff allowed to view
        for user in [self.poster, self.taker, self.staff_user]:
            self.client.login(username=user.username, password='password123')
            resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
            self.assertEqual(resp.status_code, 200)

    def test_file_size_and_type_validation(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')

        # File > 10MB
        large_content = b'x' * (10 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile('large.pdf', large_content, content_type='application/pdf')

        response = self.client.post(
            reverse('add_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [large_file]},
            follow=True
        )
        self.assertEqual(dispute.evidence.count(), 0)
        messages_list = list(response.context['messages'])
        self.assertTrue(any('exceeds maximum allowed size' in str(m) for m in messages_list))

        # Disallowed file extension/type (.exe)
        exe_file = SimpleUploadedFile('malicious.exe', b'binary content', content_type='application/x-msdownload')
        response = self.client.post(
            reverse('add_dispute_evidence', args=[dispute.id]),
            {'evidence_files': [exe_file]},
            follow=True
        )
        self.assertEqual(dispute.evidence.count(), 0)
        messages_list = list(response.context['messages'])
        self.assertTrue(any('is not allowed' in str(m) for m in messages_list))
