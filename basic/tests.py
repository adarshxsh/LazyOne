from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeEvidence, Message


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


class JurorDisputeEvidenceTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror, rewards=1000)

        self.random_user = User.objects.create_user(username='random_user', password='password123')
        UserProfile.objects.create(user=self.random_user, rewards=1000)

        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task undergoing dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but unpaid",
            deposit_amount=50,
            escrow_status='held'
        )
        Conversation.objects.create(task=self.task)

    def test_dispute_jurors_relationship(self):
        self.dispute.jurors.add(self.juror)
        self.assertIn(self.juror, self.dispute.jurors.all())

    def test_dispute_evidence_creation(self):
        evidence = DisputeEvidence.objects.create(
            dispute=self.dispute,
            uploaded_by=self.taker,
            url="https://example.com/proof.png",
            description="Proof of task completion"
        )
        self.assertEqual(evidence.dispute, self.dispute)
        self.assertIn(evidence, self.dispute.evidence_items.all())
        self.assertIn(evidence, self.dispute.evidences)

    def test_dispute_detail_authorization_for_juror(self):
        self.dispute.jurors.add(self.juror)
        self.client.login(username='juror1', password='password123')

        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_juror'])
        self.assertTrue(response.context['has_jurors'])

    def test_dispute_detail_blocked_for_non_juror(self):
        self.client.login(username='random_user', password='password123')

        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_upload_dispute_evidence_success(self):
        self.dispute.jurors.add(self.juror)
        self.client.login(username='juror1', password='password123')

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {
                'description': 'Juror review notes',
                'url': 'https://example.com/audit_report.pdf'
            }
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        evidence = DisputeEvidence.objects.filter(dispute=self.dispute, uploaded_by=self.juror).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.description, 'Juror review notes')
        self.assertEqual(evidence.url, 'https://example.com/audit_report.pdf')

    def test_upload_dispute_evidence_blocked_for_non_party(self):
        self.client.login(username='random_user', password='password123')

        response = self.client.post(
            reverse('upload_dispute_evidence', args=[self.dispute.id]),
            {
                'description': 'Unauthorized evidence',
                'url': 'https://example.com/spam.png'
            }
        )
        self.assertRedirects(response, reverse('home'))
        self.assertFalse(DisputeEvidence.objects.filter(uploaded_by=self.random_user).exists())

    def test_dispute_chat_authorization_for_juror(self):
        self.dispute.jurors.add(self.juror)
        conv, _ = Conversation.objects.get_or_create(dispute=self.dispute)

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[conv.id]))
        self.assertEqual(response.status_code, 200)

    def test_dispute_chat_blocked_for_non_juror(self):
        self.dispute.jurors.add(self.juror)
        conv, _ = Conversation.objects.get_or_create(dispute=self.dispute)

        self.client.login(username='random_user', password='password123')
        response = self.client.get(reverse('chat_view', args=[conv.id]))
        self.assertRedirects(response, reverse('home'))

    def test_send_message_in_dispute_chat_for_juror(self):
        self.dispute.jurors.add(self.juror)
        conv, _ = Conversation.objects.get_or_create(dispute=self.dispute)

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('send_message', args=[conv.id]),
            {'content': 'Juror deliberation message'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')
        self.assertTrue(Message.objects.filter(conversation=conv, sender=self.juror, content='Juror deliberation message').exists())

    def test_send_message_in_dispute_chat_blocked_for_non_juror(self):
        self.dispute.jurors.add(self.juror)
        conv, _ = Conversation.objects.get_or_create(dispute=self.dispute)

        self.client.login(username='random_user', password='password123')
        response = self.client.post(
            reverse('send_message', args=[conv.id]),
            {'content': 'Unauthorized message'}
        )
        self.assertEqual(response.status_code, 403)

    def test_template_deliberation_panel_rendering(self):
        # 1. Zero assigned jurors -> deliberation panel not rendered for jurors
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertNotContains(response, 'juror-deliberation-panel')

        # 2. Add juror -> deliberation panel rendered
        self.dispute.jurors.add(self.juror)
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertContains(response, 'juror-deliberation-panel')
        self.assertContains(response, 'Isolated Juror Deliberation Chat')


