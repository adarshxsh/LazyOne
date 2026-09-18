import math
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class UserProfileReputationTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer)

        self.client = Client()

    def _create_task(self, title, reward=100, status='in_progress', **kwargs):
        task = Task.objects.create(
            title=title, description="Description", reward=reward,
            posted_by=self.poster, taken_by=self.doer if status != 'available' else None,
            status=status, deadline=timezone.now() + timedelta(days=1),
            **kwargs
        )
        if status != 'available':
            Conversation.objects.create(task=task)
        return task

    def test_user_profile_reputation_defaults(self):
        self.assertEqual(self.doer_profile.reputation_score, 100)
        self.assertEqual(self.doer_profile.tasks_completed, 0)
        self.assertEqual(self.doer_profile.tasks_abandoned, 0)
        self.assertEqual(self.doer_profile.disputes_raised, 0)
        self.assertEqual(self.doer_profile.disputes_won, 0)
        self.assertEqual(self.doer_profile.disputes_lost, 0)
        self.assertEqual(self.doer_profile.completion_rate, 100.0)
        self.assertEqual(self.doer_profile.completion_ratio, 100.0)

    def test_complete_task_updates_reputation_and_counters(self):
        task = self._create_task("Clean room", status='in_progress')
        # Reduce doer reputation to test +5 addition
        self.doer_profile.reputation_score = 90
        self.doer_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/task/complete/{task.id}/')
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_completed, 1)
        self.assertEqual(self.doer_profile.reputation_score, 95)
        self.assertEqual(self.doer_profile.completion_rate, 100.0)

    def test_complete_task_reputation_capped_at_100(self):
        task = self._create_task("Wash car", status='in_progress')
        self.client.login(username='poster', password='password123')
        self.client.post(f'/task/complete/{task.id}/')

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_completed, 1)
        self.assertEqual(self.doer_profile.reputation_score, 100)

    def test_abandon_task_updates_reputation_and_counters(self):
        task = self._create_task("Paint wall", status='in_progress')
        self.client.login(username='doer', password='password123')
        response = self.client.post(f'/task/abandon/{task.id}/')
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_abandoned, 1)
        self.assertEqual(self.doer_profile.reputation_score, 85)
        self.assertEqual(self.doer_profile.completion_rate, 0.0)

    def test_take_task_gating_for_high_reward_task(self):
        high_val_task = self._create_task("High Value Task", reward=600, status='available')
        
        # Set low reputation for doer
        self.doer_profile.reputation_score = 75
        self.doer_profile.save()

        self.client.login(username='doer', password='password123')
        response = self.client.get(f'/task/take/{high_val_task.id}/', follow=True)

        high_val_task.refresh_from_db()
        self.assertEqual(high_val_task.status, 'available')
        self.assertIn("too low to claim high-reward tasks", response.content.decode())

        # Now increase reputation score to 85 and verify claim succeeds
        self.doer_profile.reputation_score = 85
        self.doer_profile.save()

        response = self.client.get(f'/task/take/{high_val_task.id}/', follow=True)
        high_val_task.refresh_from_db()
        self.assertEqual(high_val_task.status, 'in_progress')
        self.assertEqual(high_val_task.taken_by, self.doer)

    def test_take_task_gating_for_very_low_reputation(self):
        normal_task = self._create_task("Normal Task", reward=100, status='available')

        self.doer_profile.reputation_score = 20
        self.doer_profile.save()

        self.client.login(username='doer', password='password123')
        response = self.client.get(f'/task/take/{normal_task.id}/', follow=True)

        normal_task.refresh_from_db()
        self.assertEqual(normal_task.status, 'available')
        self.assertIn("too low to claim tasks", response.content.decode())

    def test_raise_dispute_increments_counter(self):
        task = self._create_task("Disputed Task", status='in_progress')
        self.client.login(username='doer', password='password123')

        response = self.client.post(f'/task/dispute/{task.id}/', {'reason': 'Task details missing'})
        self.assertEqual(response.status_code, 302)

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.disputes_raised, 1)

    def test_raise_dispute_gating_for_low_reputation_active_limit(self):
        task1 = self._create_task("Task 1", status='in_progress')
        task2 = self._create_task("Task 2", status='in_progress')

        self.doer_profile.reputation_score = 70
        self.doer_profile.save()

        self.client.login(username='doer', password='password123')
        # First dispute should succeed
        self.client.post(f'/task/dispute/{task1.id}/', {'reason': 'Dispute 1'})
        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.disputes_raised, 1)

        # Second dispute should fail due to active dispute limit for low reputation
        response = self.client.post(f'/task/dispute/{task2.id}/', {'reason': 'Dispute 2'}, follow=True)
        task2.refresh_from_db()
        self.assertEqual(task2.status, 'in_progress')
        self.assertIn("reached your active dispute limit", response.content.decode())

    def test_withdraw_dispute_updates_dispute_outcome_counters(self):
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

    def test_profile_views_render_reputation_metrics(self):
        self.client.login(username='doer', password='password123')
        res_private = self.client.get('/profile/')
        self.assertEqual(res_private.status_code, 200)
        self.assertIn("Reputation", res_private.content.decode())
        self.assertIn("Reliability Metrics", res_private.content.decode())

        res_public = self.client.get(f'/user/{self.doer.id}/')
        self.assertEqual(res_public.status_code, 200)
        self.assertIn("Reputation", res_public.content.decode())
        self.assertIn("Reliability", res_public.content.decode())
