from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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
class JurorDisputeEvidenceAndDeliberationTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror = User.objects.create_user(username='juror1', password='password123')
        self.juror_profile = UserProfile.objects.create(user=self.juror, rewards=1000)

        self.unassigned_user = User.objects.create_user(username='unassigned', password='password123')
        self.unassigned_profile = UserProfile.objects.create(user=self.unassigned_user, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task for testing dispute access",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        self.task_conversation = Conversation.objects.create(task=self.task)
        self.task_conversation.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work quality dispute",
            deposit_amount=50,
            status='open'
        )
        self.dispute.jurors.add(self.juror)

    def test_assigned_juror_can_access_dispute_detail(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('task_conversation', response.context)
        self.assertIn('deliberation_conversation', response.context)
        self.assertTrue(response.context['is_juror'])
        self.assertContains(response, "Task Evidence & Chat History")
        self.assertContains(response, "Dispute Deliberation Channel")

    def test_unassigned_user_gets_403_for_dispute_detail(self):
        self.client.login(username='unassigned', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 403)

    def test_assigned_juror_read_only_task_chat(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.task_conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Attempt to send message to task chat as juror
        send_resp = self.client.post(
            reverse('send_message', args=[self.task_conversation.id]),
            {'content': 'Juror trying to post to task chat'}
        )
        self.assertEqual(send_resp.status_code, 403)

    def test_unassigned_user_gets_403_for_task_chat_and_deliberation(self):
        delib_conv = self.dispute.get_or_create_deliberation_conversation()
        self.client.login(username='unassigned', password='password123')

        # Task chat access check
        resp1 = self.client.get(reverse('chat_view', args=[self.task_conversation.id]))
        self.assertEqual(resp1.status_code, 403)

        send_resp1 = self.client.post(
            reverse('send_message', args=[self.task_conversation.id]),
            {'content': 'Unauthorized post'}
        )
        self.assertEqual(send_resp1.status_code, 403)

        # Deliberation channel access check
        resp2 = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(resp2.status_code, 403)

        send_resp2 = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'Unauthorized post'}
        )
        self.assertEqual(send_resp2.status_code, 403)

    def test_juror_deliberation_channel_open_and_resolved(self):
        delib_conv = self.dispute.get_or_create_deliberation_conversation()
        self.client.login(username='juror1', password='password123')

        # Open dispute: Juror can view and post
        resp = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['is_read_only'])

        send_resp = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'Juror deliberation message'}
        )
        self.assertEqual(send_resp.status_code, 200)

        # Transition dispute to resolved
        self.dispute.status = 'resolved'
        self.dispute.save()

        # Resolved dispute: Deliberation channel becomes read-only
        resp_resolved = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(resp_resolved.status_code, 200)
        self.assertTrue(resp_resolved.context['is_read_only'])

        send_resp_resolved = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'Post after resolution'}
        )
        self.assertEqual(send_resp_resolved.status_code, 403)


