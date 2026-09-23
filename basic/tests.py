from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeJuror, Message


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


class DisputeJurorTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.unauthorized_user = User.objects.create_user(username='unauthorized', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        UserProfile.objects.create(user=self.juror1, rewards=1000)
        UserProfile.objects.create(user=self.juror2, rewards=1000)
        UserProfile.objects.create(user=self.unauthorized_user, rewards=1000)

        # Task & Conversation
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        self.main_chat = Conversation.objects.create(task=self.task)
        self.main_chat.participants.add(self.poster, self.taker)

        # Dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unresolved issue regarding work done",
            deposit_amount=50,
            escrow_status='held'
        )

        # Dispute Juror Assignment
        self.dispute_juror1 = DisputeJuror.objects.create(dispute=self.dispute, user=self.juror1)
        self.dispute_juror2 = DisputeJuror.objects.create(dispute=self.dispute, user=self.juror2)

    def test_dispute_juror_creation_and_deliberation_conversation(self):
        self.assertIsNotNone(self.dispute.deliberation_conversation)
        self.assertIn(self.juror1, self.dispute.deliberation_conversation.participants.all())
        self.assertIn(self.juror2, self.dispute.deliberation_conversation.participants.all())
        self.assertNotIn(self.poster, self.dispute.deliberation_conversation.participants.all())
        self.assertNotIn(self.taker, self.dispute.deliberation_conversation.participants.all())

    def test_dispute_detail_access_and_rendering(self):
        # Assigned juror can view dispute details
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('chat_view', args=[self.main_chat.id]))
        self.assertContains(response, reverse('chat_view', args=[self.dispute.deliberation_conversation.id]))

        # Unauthorized user gets redirected to home
        self.client.login(username='unauthorized', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_juror_read_only_main_chat_access(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.main_chat.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])
        self.assertContains(response, "Read-Only Evidence Review")

    def test_juror_forbidden_from_posting_in_main_chat(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('send_message', args=[self.main_chat.id]),
            {'content': 'Juror attempting to write in main chat'}
        )
        self.assertEqual(response.status_code, 403)

    def test_juror_deliberation_chat_messaging(self):
        deliberation_conv = self.dispute.deliberation_conversation

        # Juror 1 sends message
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('send_message', args=[deliberation_conv.id]),
            {'content': 'I think the task doer provided valid proof.'}
        )
        self.assertEqual(response.status_code, 200)

        msg = Message.objects.filter(conversation=deliberation_conv, sender=self.juror1).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, 'I think the task doer provided valid proof.')

        # Juror 2 views deliberation chat
        self.client.login(username='juror2', password='password123')
        response = self.client.get(reverse('chat_view', args=[deliberation_conv.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_read_only'])

    def test_parties_forbidden_from_deliberation_chat(self):
        deliberation_conv = self.dispute.deliberation_conversation

        # Task Poster
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('chat_view', args=[deliberation_conv.id]))
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            reverse('send_message', args=[deliberation_conv.id]),
            {'content': 'Poster trying to intervene'}
        )
        self.assertEqual(response.status_code, 403)

        # Task Doer
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('chat_view', args=[deliberation_conv.id]))
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            reverse('send_message', args=[deliberation_conv.id]),
            {'content': 'Taker trying to intervene'}
        )
        self.assertEqual(response.status_code, 403)


