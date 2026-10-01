from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeEvidence


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeEvidenceAndJurorDeliberationTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror = User.objects.create_user(username='juror1', password='password123')
        self.juror_profile = UserProfile.objects.create(user=self.juror, rewards=1000)

        self.unassigned = User.objects.create_user(username='unassigned', password='password123')
        self.unassigned_profile = UserProfile.objects.create(user=self.unassigned, rewards=1000)

        self.task = Task.objects.create(
            title="Deliberation Test Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.task_chat = Conversation.objects.create(task=self.task)
        self.task_chat.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work quality dispute",
            deposit_amount=50,
            status='open'
        )
        self.dispute.jurors.add(self.juror)
        self.deliberation_chat = self.dispute.get_or_create_deliberation_conversation()

    def test_assigned_juror_can_view_dispute_details_and_access_task_chat_history(self):
        self.client.login(username='juror1', password='password123')

        # View dispute detail
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_juror'])

        # View task chat history (read-only)
        response = self.client.get(reverse('chat_view', args=[self.task_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

    def test_juror_cannot_post_to_main_task_chat(self):
        self.client.login(username='juror1', password='password123')

        response = self.client.post(
            reverse('send_message', args=[self.task_chat.id]),
            {'content': 'Juror attempting to post in task chat'}
        )
        self.assertEqual(response.status_code, 403)

    def test_unassigned_user_cannot_access_deliberation_chat(self):
        self.client.login(username='unassigned', password='password123')

        # View deliberation chat -> redirect to home
        response = self.client.get(reverse('chat_view', args=[self.deliberation_chat.id]))
        self.assertRedirects(response, reverse('home'))

        # Send message to deliberation chat -> 403
        response = self.client.post(
            reverse('send_message', args=[self.deliberation_chat.id]),
            {'content': 'Unassigned user trying to deliberate'}
        )
        self.assertEqual(response.status_code, 403)

    def test_juror_can_upload_evidence_and_deliberate_in_deliberation_chat(self):
        self.client.login(username='juror1', password='password123')

        # Upload evidence
        response = self.client.post(
            reverse('dispute_detail', args=[self.dispute.id]),
            {
                'file_url': 'http://example.com/screenshot.png',
                'description': 'Log evidence showing incomplete delivery'
            }
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        evidence = DisputeEvidence.objects.filter(dispute=self.dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.uploaded_by, self.juror)
        self.assertEqual(evidence.description, 'Log evidence showing incomplete delivery')

        # View deliberation chat
        response = self.client.get(reverse('chat_view', args=[self.deliberation_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_read_only'])

        # Post message in deliberation chat
        response = self.client.post(
            reverse('send_message', args=[self.deliberation_chat.id]),
            {'content': 'Evidence seems clear in favor of taker.'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.deliberation_chat.messages.filter(sender=self.juror, content='Evidence seems clear in favor of taker.').exists())

    def test_deliberation_chat_becomes_read_only_when_dispute_resolved_or_withdrawn(self):
        self.client.login(username='juror1', password='password123')

        # Resolve dispute
        self.dispute.status = 'resolved'
        self.dispute.save()

        # View deliberation chat
        response = self.client.get(reverse('chat_view', args=[self.deliberation_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Post message in deliberation chat -> 403
        response = self.client.post(
            reverse('send_message', args=[self.deliberation_chat.id]),
            {'content': 'Post after resolution'}
        )
        self.assertEqual(response.status_code, 403)


