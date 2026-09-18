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


class UserProfileReputationTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer)

        self.client = Client()

    def _create_task(self, title, status='in_progress', **kwargs):
        task = Task.objects.create(
            title=title, description="Description", reward=100,
            posted_by=self.poster, taken_by=self.doer if status != 'available' else None,
            status=status, deadline=timezone.now() + timedelta(days=1),
            **kwargs
        )
        if status != 'available':
            Conversation.objects.create(task=task)
        return task

    def test_reputation_default_values(self):
        self.assertEqual(self.doer_profile.reputation_score, 100.0)
        self.assertEqual(self.doer_profile.tasks_completed, 0)
        self.assertEqual(self.doer_profile.tasks_abandoned, 0)
        self.assertEqual(self.doer_profile.disputes_won, 0)
        self.assertEqual(self.doer_profile.disputes_lost, 0)
        self.assertEqual(self.doer_profile.reliability_percentage, 100.0)

    def test_task_completion_increases_score_and_counter(self):
        task = self._create_task("Clean room", status='in_progress')
        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/task/complete/{task.id}/')
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_completed, 1)
        self.assertEqual(self.doer_profile.reputation_score, 110.0)
        self.assertEqual(self.doer_profile.reliability_percentage, 100.0)

    def test_task_abandonment_decreases_score_and_increases_abandoned_counter(self):
        task = self._create_task("Paint wall", status='in_progress')
        self.client.login(username='doer', password='password123')
        response = self.client.post(f'/task/abandon/{task.id}/')
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_abandoned, 1)
        self.assertEqual(self.doer_profile.reputation_score, 80.0)
        self.assertEqual(self.doer_profile.reliability_percentage, 0.0)

    def test_dispute_resolution_updates_stats(self):
        task = self._create_task("Fix bike", status='in_progress')
        self.client.login(username='doer', password='password123')
        self.client.post(f'/task/dispute/{task.id}/', {'reason': 'Poster not responding'})

        self.client.login(username='poster', password='password123')
        self.client.post(f'/task/complete/{task.id}/')

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.doer_profile.tasks_completed, 1)
        self.assertEqual(self.doer_profile.disputes_won, 1)
        self.assertEqual(self.poster_profile.disputes_lost, 1)

    def test_dispute_withdrawal_updates_stats(self):
        task = self._create_task("Mow lawn", status='in_progress')
        self.client.login(username='doer', password='password123')
        self.client.post(f'/task/dispute/{task.id}/', {'reason': 'Misunderstanding'})
        task.refresh_from_db()

        dispute = task.dispute
        response = self.client.post(f'/dispute/withdraw/{dispute.id}/')
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.doer_profile.disputes_lost, 1)
        self.assertEqual(self.poster_profile.disputes_won, 1)

    def test_task_claim_gating_for_low_reputation(self):
        task = self._create_task("Delivery", status='available')
        self.doer_profile.reputation_score = 40.0
        self.doer_profile.save()

        self.client.login(username='doer', password='password123')
        response = self.client.get(f'/task/take/{task.id}/', follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIn("below the required threshold", response.content.decode())

    def test_raise_dispute_gating_for_low_reputation(self):
        task = self._create_task("Tutoring", status='in_progress')
        self.doer_profile.reputation_score = 45.0
        self.doer_profile.save()

        self.client.login(username='doer', password='password123')
        response = self.client.post(f'/task/dispute/{task.id}/', {'reason': 'Invalid'}, follow=True)

        self.assertFalse(hasattr(task, 'dispute'))
        self.assertIn("below the required threshold", response.content.decode())

    def test_baseline_initialization_for_existing_active_users(self):
        self._create_task("Past 1", status='completed')
        self._create_task("Past 2", status='completed')

        self.assertEqual(self.doer_profile.tasks_completed, 0)
        self.doer_profile.initialize_baseline_reputation()

        self.assertEqual(self.doer_profile.tasks_completed, 2)
        self.assertEqual(self.doer_profile.reputation_score, 120.0)

    def test_profile_views_render_reputation_metrics(self):
        self.client.login(username='doer', password='password123')
        res_private = self.client.get('/profile/')
        self.assertEqual(res_private.status_code, 200)
        self.assertIn("Reputation & Reliability Metrics", res_private.content.decode())

        res_public = self.client.get(f'/user/{self.doer.id}/')
        self.assertEqual(res_public.status_code, 200)
        self.assertIn("Reputation & Reliability", res_public.content.decode())
