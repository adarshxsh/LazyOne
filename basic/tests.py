from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation, Notification


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
        self.assertEqual(dispute.status, 'withdrawn')

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


class AutomatedDisputeSettlementTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )

    def test_raise_dispute_with_file_attachment_and_expiration(self):
        self.client.login(username='taker', password='password123')
        evidence_file = SimpleUploadedFile("evidence.txt", b"Proof of work done.", content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster refused to accept completion.', 'evidence_files': evidence_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = self.task.dispute
        self.assertIsNotNone(dispute)
        self.assertEqual(dispute.status, 'open')
        self.assertIsNotNone(dispute.expires_at)
        self.assertGreater(dispute.expires_at, timezone.now())

        # Check evidence attached
        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        self.assertTrue(evidences.first().file.name.startswith('dispute_evidence/'))

    def test_file_size_exceeds_10mb_limit(self):
        self.client.login(username='taker', password='password123')
        # Create dummy file > 10MB
        large_content = b"x" * (10 * 1024 * 1024 + 10)
        large_file = SimpleUploadedFile("large.txt", large_content, content_type="text/plain")

        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Too big file.', 'evidence_files': large_file},
            follow=True
        )
        self.assertContains(response, "File attachments must not exceed 10 MB per upload.")
        self.assertIsNone(self.task.dispute)

    def test_submit_counter_evidence(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Poster submits counter-evidence
        self.client.login(username='poster', password='password123')
        counter_file = SimpleUploadedFile("counter.txt", b"Counter proof.", content_type="text/plain")
        response = self.client.post(
            reverse('submit_dispute_evidence', args=[dispute.id]),
            {'content': 'I disagree with taker.', 'evidence_files': counter_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        self.assertEqual(evidences.first().sender, self.poster)
        self.assertEqual(evidences.first().content, 'I disagree with taker.')

        # Verify notification sent to taker
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_withdraw_dispute_soft_deletes_record(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        # Confirm record was NOT hard-deleted from database
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_auto_resolve_expired_dispute_taker_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Expired dispute test'}, follow=True)
        dispute = self.task.dispute

        # Set expiration date to the past
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Trigger background webhook
        response = self.client.get(reverse('auto_resolve_disputes'))
        self.assertEqual(response.status_code, 200)
        json_resp = response.json()
        self.assertEqual(json_resp['status'], 'success')
        self.assertEqual(json_resp['processed_count'], 1)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'auto_resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check taker received reward + deposit refund (500 - 50 deposit + 200 task reward + 50 deposit refund = 700)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        # Check ledger audit entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_settlement'
        ).first()
        self.assertIsNotNone(ledger_entry)

        # Check notifications sent to both users
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_auto_resolve_expired_dispute_poster_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Expired dispute test'}, follow=True)
        dispute = self.task.dispute

        # Poster responds with evidence
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('submit_dispute_evidence', args=[dispute.id]), {'content': 'Poster counter-evidence'}, follow=True)

        # Set expiration date to the past (taker failed to respond after poster's evidence)
        dispute.expires_at = timezone.now() - timedelta(hours=1)
        dispute.save()

        # Trigger background webhook
        response = self.client.get(reverse('auto_resolve_disputes'))
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'auto_resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Check poster refunded reward
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 + 200

        # Check ledger audit entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.poster, task=self.task, transaction_type='dispute_settlement'
        ).first()
        self.assertIsNotNone(ledger_entry)

    def test_manual_task_completion_overrides_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'}, follow=True)
        dispute = self.task.dispute

        # Poster manually completes task during active dispute
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('complete_task', args=[self.task.id]), follow=True)
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)
