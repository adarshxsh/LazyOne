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


class JurorChatAndDeliberationTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror = User.objects.create_user(username='juror', password='password123')
        self.juror_profile = UserProfile.objects.create(user=self.juror, rewards=1000)

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        self.random_user = User.objects.create_user(username='random_user', password='password123')
        UserProfile.objects.create(user=self.random_user, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task with dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        self.task_chat = Conversation.objects.create(task=self.task)
        self.task_chat.participants.add(self.poster, self.taker)

    def test_read_only_transcript_and_messaging_guard_for_disputed_tasks(self):
        # 1. Non-disputed task chat guard
        self.client.login(username='random_user', password='password123')
        res = self.client.get(reverse('chat_view', args=[self.task_chat.id]))
        self.assertRedirects(res, reverse('home'))

        # 2. Raise dispute
        self.client.login(username='taker', password='password123')
        res = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with deliverable'})
        self.assertEqual(res.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute
        dispute.jurors.add(self.juror)

        # 3. Juror and Random Community Member can view transcript read-only
        for username in ['juror', 'random_user', 'poster', 'taker']:
            self.client.login(username=username, password='password123')
            res = self.client.get(reverse('chat_view', args=[self.task_chat.id]))
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.context['is_read_only'])

        # 4. Attempting to send message in main task chat during dispute is forbidden for everyone
        for username in ['juror', 'random_user', 'poster', 'taker']:
            self.client.login(username=username, password='password123')
            res = self.client.post(reverse('send_message', args=[self.task_chat.id]), {'content': 'Hello'})
            self.assertEqual(res.status_code, 403)

    def test_deliberation_channel_creation_and_permissions(self):
        # Raise dispute to auto-create deliberation channel
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Deliberation test'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertIsNotNone(dispute.deliberation_channel)
        delib_id = dispute.deliberation_channel.id

        dispute.jurors.add(self.juror)

        # Task participants (poster & taker) are excluded from deliberation channel while open
        for username in ['poster', 'taker', 'random_user']:
            self.client.login(username=username, password='password123')
            res = self.client.get(reverse('chat_view', args=[delib_id]))
            self.assertRedirects(res, reverse('home'))

            res_post = self.client.post(reverse('send_message', args=[delib_id]), {'content': 'Unauthorized note'})
            self.assertEqual(res_post.status_code, 403)

        # Assigned juror can view and send messages in deliberation channel
        self.client.login(username='juror', password='password123')
        res_juror_view = self.client.get(reverse('chat_view', args=[delib_id]))
        self.assertEqual(res_juror_view.status_code, 200)
        self.assertFalse(res_juror_view.context['is_read_only'])

        res_juror_msg = self.client.post(reverse('send_message', args=[delib_id]), {'content': 'Juror note on evidence'})
        self.assertEqual(res_juror_msg.status_code, 200)

        # Staff can view and send messages in deliberation channel
        self.client.login(username='staff', password='password123')
        res_staff_view = self.client.get(reverse('chat_view', args=[delib_id]))
        self.assertEqual(res_staff_view.status_code, 200)

        res_staff_msg = self.client.post(reverse('send_message', args=[delib_id]), {'content': 'Staff note on evidence'})
        self.assertEqual(res_staff_msg.status_code, 200)

