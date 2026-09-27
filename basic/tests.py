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


from django.core.files.uploadedfile import SimpleUploadedFile
from .models import DisputeEvidence

class DisputeJurorEvidenceChatTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror = User.objects.create_user(username='juror1', password='password123')
        self.other_user = User.objects.create_user(username='other_user', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.juror, rewards=1000)
        UserProfile.objects.create(user=self.other_user, rewards=1000)

        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )

        self.main_chat = Conversation.objects.create(task=self.task, conversation_type='main')
        self.main_chat.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unclear requirements",
            deposit_amount=50,
            escrow_status='held'
        )
        self.dispute.jurors.add(self.juror)

    def test_juror_access_dispute_detail(self):
        # Assigned juror can access dispute detail page
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Unauthorized user is redirected to home
        self.client.login(username='other_user', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_juror_read_only_main_task_chat(self):
        # Juror can view main task chat in read-only mode
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.main_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Juror is blocked from sending messages in main task chat
        response = self.client.post(
            reverse('send_message', args=[self.main_chat.id]),
            {'content': 'Juror trying to speak'}
        )
        self.assertEqual(response.status_code, 403)

        # Taker can send messages in main task chat
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('send_message', args=[self.main_chat.id]),
            {'content': 'Hello poster'}
        )
        self.assertEqual(response.status_code, 200)

    def test_evidence_file_upload(self):
        self.client.login(username='taker', password='password123')
        sample_file = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        
        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'title': 'Proof Screenshot', 'file': sample_file}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        
        evidence = DisputeEvidence.objects.filter(dispute=self.dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.title, 'Proof Screenshot')
        self.assertEqual(evidence.uploaded_by, self.taker)

        # Invalid format test
        invalid_file = SimpleUploadedFile("script.exe", b"binary", content_type="application/x-msdownload")
        response = self.client.post(
            reverse('upload_evidence', args=[self.dispute.id]),
            {'title': 'Bad File', 'file': invalid_file}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(title='Bad File').count(), 0)

    def test_juror_deliberation_chat(self):
        deliberation_chat = self.dispute.deliberation_chat

        # Assigned juror can view deliberation chat
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[deliberation_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_read_only'])

        # Assigned juror can post in deliberation chat
        response = self.client.post(
            reverse('send_message', args=[deliberation_chat.id]),
            {'content': 'Deliberating with peer jurors'}
        )
        self.assertEqual(response.status_code, 200)

        # Task participant (poster/taker) is blocked from deliberation chat
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('chat_view', args=[deliberation_chat.id]))
        self.assertRedirects(response, reverse('home'))

        response = self.client.post(
            reverse('send_message', args=[deliberation_chat.id]),
            {'content': 'Poster trying to view deliberation'}
        )
        self.assertEqual(response.status_code, 403)


