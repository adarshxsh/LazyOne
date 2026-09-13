from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation

class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_insufficient_points(self):
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task poster unresponsive'})

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').exists())

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task poster unresponsive'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.deposit_amount, 50)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50)

        ledger = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_deposit')
        self.assertEqual(ledger.amount, -50)
        self.assertEqual(ledger.task, self.task)

    def test_withdraw_dispute_success(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Issue with task'})
        
        dispute = Dispute.objects.get(task=self.task)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        refund_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_refund')
        self.assertEqual(refund_ledger.amount, 50)
        self.assertEqual(refund_ledger.task, self.task)

    def test_complete_task_with_open_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute raised'})

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        # Initial 100 - 50 (deposit) + 200 (task reward) + 50 (deposit refund) = 300
        self.assertEqual(self.taker_profile.rewards, 300)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund', amount=50).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion', amount=200).exists())

    def test_raise_dispute_exact_50_points(self):
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Exact balance test'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 0)

    def test_raise_dispute_unauthorized_user(self):
        other_user = User.objects.create_user(username='other', password='password123')
        UserProfile.objects.create(user=other_user, rewards=1000)

        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unauthorized dispute'})

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
