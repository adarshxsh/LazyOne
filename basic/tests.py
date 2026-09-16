from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, RewardLedger, Conversation

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeDepositTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_insufficient_rewards(self):
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'},
            follow=True
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.assertEqual(Dispute.objects.count(), 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50)

    def test_raise_dispute_success_reserves_deposit_and_logs_ledger(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster is unresponsive'},
            follow=True
        )

        self.assertEqual(Dispute.objects.count(), 1)
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.deposit_amount, 100)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1400) # 1500 - 100

        ledger_entry = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_reserved'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -100)
        self.assertEqual(ledger_entry.task, self.task)

    def test_withdraw_dispute_refunds_deposit(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Need clarification'},
            follow=True
        )

        dispute = Dispute.objects.get(task=self.task)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1400)

        withdraw_response = self.client.post(
            reverse('withdraw_dispute', args=[dispute.id]),
            follow=True
        )

        self.assertRedirects(withdraw_response, reverse('my_tasks'))
        self.assertEqual(Dispute.objects.count(), 0)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1500) # 1400 + 100

        refund_entry = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_refund'
        ).first()
        self.assertIsNotNone(refund_entry)
        self.assertEqual(refund_entry.amount, 100)

    def test_resolve_dispute_on_task_completion_refunds_deposit(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Waiting for approval'},
            follow=True
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('complete_task', args=[self.task.id]),
            follow=True
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        # Initial 1500 - 100 deposit + 500 reward + 100 deposit refund = 2000
        self.assertEqual(self.taker_profile.rewards, 2000)

        refund_entry = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_refund'
        ).first()
        self.assertIsNotNone(refund_entry)
        self.assertEqual(refund_entry.amount, 100)

    @override_settings(DISPUTE_DEPOSIT=150)
    def test_configurable_dispute_deposit(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Testing custom deposit'},
            follow=True
        )

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1350) # 1500 - 150

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 150)

        reserved_entry = RewardLedger.objects.get(
            user=self.taker,
            transaction_type='dispute_deposit_reserved'
        )
        self.assertEqual(reserved_entry.amount, -150)

        self.client.post(
            reverse('withdraw_dispute', args=[dispute.id]),
            follow=True
        )

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1500) # Restored 1500
