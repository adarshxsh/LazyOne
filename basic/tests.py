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


class UserReputationAndRiskTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=1)

    def test_default_reputation_score(self):
        """Verify default initial reputation score of 100 for new user profiles."""
        new_user = User.objects.create_user(username='newbie', password='password123')
        new_profile = UserProfile.objects.create(user=new_user)
        self.assertEqual(new_profile.reputation_score, 100)
        self.assertEqual(new_profile.dispute_fraud_risk_index, 0.0)
        self.assertEqual(new_profile.total_tasks_taken, 0)
        self.assertEqual(new_profile.tasks_completed, 0)
        self.assertEqual(new_profile.tasks_abandoned, 0)
        self.assertEqual(new_profile.disputes_raised, 0)

    def test_taking_task_increments_total_tasks_taken(self):
        """Taking a task increments total_tasks_taken on taker profile."""
        task = Task.objects.create(
            title="Clean Room", description="Detail clean", reward=100,
            posted_by=self.poster, deadline=self.deadline, status='available'
        )
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.total_tasks_taken, 1)

    def test_completing_task_increments_completed_and_increases_reputation(self):
        """Completing a task increments tasks_completed and increases reputation score."""
        task = Task.objects.create(
            title="Math Homework", description="Help with calculus", reward=100,
            posted_by=self.poster, taken_by=self.taker, deadline=self.deadline, status='in_progress'
        )
        self.taker_profile.total_tasks_taken = 1
        self.taker_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.tasks_completed, 1)
        self.assertGreater(self.taker_profile.reputation_score, 100)

    def test_abandoning_task_increments_abandoned_and_decreases_reputation(self):
        """Abandoning a task increments tasks_abandoned and decreases reputation score (capped at 0)."""
        task = Task.objects.create(
            title="Moving Help", description="Carry boxes", reward=100,
            posted_by=self.poster, taken_by=self.taker, deadline=self.deadline, status='in_progress'
        )
        self.taker_profile.reputation_score = 15
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.tasks_abandoned, 1)
        self.assertEqual(self.taker_profile.reputation_score, 80)

        # Test cap at 0 minimum
        self.taker_profile.tasks_abandoned = 10
        self.taker_profile.recalculate_reputation_and_risk()
        self.taker_profile.save()
        self.assertEqual(self.taker_profile.reputation_score, 0)

    def test_raising_dispute_updates_dispute_metrics_and_risk_index(self):
        """Raising a dispute updates disputes_raised counter and recalculates fraud risk index."""
        task = Task.objects.create(
            title="Design Logo", description="Create SVG logo", reward=200,
            posted_by=self.poster, taken_by=self.taker, deadline=self.deadline, status='in_progress'
        )
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Unreasonable request'})
        self.assertRedirects(response, reverse('dispute_detail', args=[task.dispute.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.disputes_raised, 1)
        self.assertGreater(self.taker_profile.dispute_fraud_risk_index, 0.0)

    def test_access_control_blocks_low_reputation_from_high_risk_tasks(self):
        """Users with low reputation score or high fraud risk cannot take high-value tasks."""
        high_value_task = Task.objects.create(
            title="Build Website", description="Full stack app", reward=600,
            posted_by=self.poster, deadline=self.deadline, status='available'
        )
        # Set taker profile reputation score below 50
        self.taker_profile.reputation_score = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[high_value_task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        high_value_task.refresh_from_db()
        self.assertIsNone(high_value_task.taken_by)
        self.assertEqual(high_value_task.status, 'available')
