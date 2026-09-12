from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, RewardLedger, calculate_deposit_bond

class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=50)

        self.poor_taker = User.objects.create_user(username='poor_taker', password='password123')
        self.poor_profile = UserProfile.objects.create(user=self.poor_taker, rewards=5)

        self.task = Task.objects.create(
            title="Test Task",
            description="Task Description",
            reward=100, # Required deposit bond = 20 points
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

        self.client = Client()

    def test_deposit_bond_calculation(self):
        self.assertEqual(calculate_deposit_bond(100), 20)
        self.assertEqual(calculate_deposit_bond(50), 10)
        self.assertEqual(calculate_deposit_bond(3), 1)
        self.assertEqual(calculate_deposit_bond(0), 1)
        self.assertEqual(self.task.get_deposit_bond(), 20)

    def test_insufficient_balance_blocks_dispute(self):
        poor_task = Task.objects.create(
            title="Poor Task",
            description="Task Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.poor_taker,
            status='in_progress'
        )
        self.client.login(username='poor_taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[poor_task.id]), {'reason': 'Unfair conditions'})

        poor_task.refresh_from_db()
        self.assertEqual(poor_task.status, 'in_progress')
        self.assertFalse(hasattr(poor_task, 'dispute'))
        self.poor_profile.refresh_from_db()
        self.assertEqual(self.poor_profile.rewards, 5)

    def test_raise_dispute_deducts_bond_and_logs_hold(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task details dispute'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        dispute = self.task.dispute
        self.assertEqual(dispute.deposit_amount, 20)
        self.assertEqual(dispute.deposit_status, 'held')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30) # 50 - 20 = 30

        hold_ledger = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_hold',
            task=self.task
        ).first()
        self.assertIsNotNone(hold_ledger)
        self.assertEqual(hold_ledger.amount, -20)

    def test_withdraw_dispute_refunds_bond_and_logs_refund(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task details dispute'})

        dispute = self.task.dispute
        dispute_id = dispute.id

        response = self.client.post(reverse('withdraw_dispute', args=[dispute_id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute_id).exists())

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 50) # Refunded back to 50

        refund_ledger = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_refund',
            task=self.task
        ).first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 20)

    def test_complete_disputed_task_refunds_deposit_bond(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task details dispute'})

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.task.dispute.status, 'resolved')
        self.assertEqual(self.task.dispute.deposit_status, 'refunded')

        self.taker_profile.refresh_from_db()
        # Initial 50 - 20 (hold) + 100 (completion reward) + 20 (refund) = 150
        self.assertEqual(self.taker_profile.rewards, 150)

        refund_ledger = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_deposit_refund',
            task=self.task
        ).first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 20)

    def test_resolve_dispute_forfeit_bond(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Spam dispute'})

        dispute = self.task.dispute

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'outcome': 'forfeit'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.deposit_status, 'forfeited')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30) # No refund granted

        forfeit_ledger = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='dispute_bond_forfeiture',
            task=self.task
        ).first()
        self.assertIsNotNone(forfeit_ledger)
        self.assertEqual(forfeit_ledger.amount, -20)

    def test_rewards_dashboard_displays_dispute_transactions(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dashboard test'})

        dispute = self.task.dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dispute deposit hold')
        self.assertContains(response, 'Dispute deposit refund')
