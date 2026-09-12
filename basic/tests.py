from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation
from django.utils import timezone
from datetime import timedelta

class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 100})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client = Client()

    def test_calculate_deposit_bond(self):
        """Verify calculation of 20% deposit bond with minimum 10 points floor."""
        # Task reward 100 -> 20 points
        task100 = Task(reward=100)
        self.assertEqual(task100.calculate_deposit_bond(), 20)

        # Task reward 50 -> 10 points
        task50 = Task(reward=50)
        self.assertEqual(task50.calculate_deposit_bond(), 10)

        # Task reward 30 -> 10 points (floor)
        task30 = Task(reward=30)
        self.assertEqual(task30.calculate_deposit_bond(), 10)

        # Task reward 10 -> 10 points (floor)
        task10 = Task(reward=10)
        self.assertEqual(task10.calculate_deposit_bond(), 10)

        # Task reward 125 -> 25 points
        task125 = Task(reward=125)
        self.assertEqual(task125.calculate_deposit_bond(), 25)

        # Dispute model static method
        self.assertEqual(Dispute.calculate_deposit_bond(100), 20)
        self.assertEqual(Dispute.calculate_deposit_bond(15), 10)

    def test_raise_dispute_insufficient_points_fails(self):
        """Raising a dispute fails if user has fewer points than required bond."""
        # Set taker rewards to 5 (less than 20 points required for 100 reward task)
        self.taker_profile.rewards = 5
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 5)

        # Check error message
        messages = list(response.wsgi_request._messages)
        self.assertTrue(any('deposit bond of 20 points' in str(m) for m in messages))

    def test_raise_dispute_success_deducts_bond_and_creates_ledger(self):
        """Raising a dispute deducts bond, creates Dispute model with held status, and logs RewardLedger entry."""
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        dispute = self.task.dispute
        self.assertEqual(dispute.deposit_amount, 20)
        self.assertEqual(dispute.bond_status, 'held')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 80) # 100 - 20

        # Verify ledger hold entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='dispute_deposit_hold'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)

    def test_withdraw_dispute_refunds_bond_and_creates_ledger(self):
        """Withdrawing a dispute refunds deposit_amount and logs dispute_deposit_refund in RewardLedger."""
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100) # 80 + 20

        # Verify ledger refund entry
        refund_entry = RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='dispute_deposit_refund'
        ).first()
        self.assertIsNotNone(refund_entry)
        self.assertEqual(refund_entry.amount, 20)

    def test_complete_task_with_active_dispute_refunds_bond(self):
        """Completing a task with an active dispute refunds the taker's deposit bond and marks bond_status='refunded'."""
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.bond_status, 'refunded')

        self.taker_profile.refresh_from_db()
        # Initial 100 - 20 (hold) + 100 (task reward) + 20 (deposit refund) = 200
        self.assertEqual(self.taker_profile.rewards, 200)

        refund_entry = RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='dispute_deposit_refund'
        ).first()
        self.assertIsNotNone(refund_entry)
        self.assertEqual(refund_entry.amount, 20)

    def test_backwards_compatibility_old_dispute_zero_deposit(self):
        """Existing disputes created prior to bond enforcement (0 deposit) resolve gracefully without errors."""
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Old dispute',
            deposit_amount=0,
            bond_status='held'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 200) # 100 + 100 task reward, 0 bond refund
