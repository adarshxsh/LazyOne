from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, ReputationLog
from .services.reputation import ReputationService


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


class UserReputationGovernanceTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.poster_profile.rewards = 2000
        self.poster_profile.reputation_score = 100
        self.poster_profile.risk_tier = 'LOW'
        self.poster_profile.save()

        self.doer = User.objects.create_user(username='doer_user', password='password123')
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer)
        self.doer_profile.rewards = 1000
        self.doer_profile.reputation_score = 100
        self.doer_profile.risk_tier = 'LOW'
        self.doer_profile.save()

        self.client = Client()

    def test_default_user_profile_reputation_schema_and_defaults(self):
        user = User.objects.create_user(username='new_user', password='password123')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        self.assertEqual(profile.reputation_score, 100)
        self.assertEqual(profile.risk_tier, 'LOW')
        self.assertEqual(profile.completed_tasks_count, 0)
        self.assertEqual(profile.abandoned_tasks_count, 0)
        self.assertEqual(profile.disputes_raised_count, 0)
        self.assertEqual(profile.disputes_won_count, 0)
        self.assertEqual(profile.disputes_lost_count, 0)
        self.assertEqual(profile.completion_percentage, 100.0)
        self.assertEqual(profile.dispute_win_rate, 100.0)

    def test_task_completion_increments_completed_count_and_increases_reputation(self):
        self.client.login(username='poster_user', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'Test Completion Task',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Test Completion Task')

        # Doer claims task
        self.client.login(username='doer_user', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster marks complete
        self.client.login(username='poster_user', password='password123')
        self.client.get(reverse('complete_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.completed_tasks_count, 1)
        self.assertEqual(self.doer_profile.reputation_score, 110) # 100 + 10

    def test_task_abandonment_increments_abandoned_count_and_decrements_reputation(self):
        self.client.login(username='poster_user', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'Task To Abandon',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Task To Abandon')

        # Doer claims task
        self.client.login(username='doer_user', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Doer abandons task
        self.client.get(reverse('abandon_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.abandoned_tasks_count, 1)
        self.assertEqual(self.doer_profile.reputation_score, 75) # 100 - 25
        self.assertEqual(self.doer_profile.risk_tier, 'MEDIUM')

    def test_dispute_resolution_updates_win_loss_counters_and_risk_tier(self):
        self.client.login(username='poster_user', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'Disputed Task',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Disputed Task')

        # Doer claims task and raises dispute
        self.client.login(username='doer_user', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Payment issue'})

        dispute = Dispute.objects.get(task=task)

        # Poster resolves dispute in favor of doer
        self.client.login(username='poster_user', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'doer'})

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.doer_profile.disputes_won_count, 1)
        self.assertEqual(self.doer_profile.reputation_score, 115) # 100 + 15

        self.assertEqual(self.poster_profile.disputes_lost_count, 1)
        self.assertEqual(self.poster_profile.reputation_score, 70) # 100 - 30
        self.assertEqual(self.poster_profile.risk_tier, 'MEDIUM')

    def test_governance_engine_enforces_collateral_and_claiming_limits(self):
        # Set doer to HIGH risk tier
        self.doer_profile.reputation_score = 45
        self.doer_profile.risk_tier = 'HIGH'
        self.doer_profile.rewards = 200
        self.doer_profile.save()

        self.client.login(username='poster_user', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'High Collateral Task',
            'description': 'Description',
            'reward': '200',
            'deadline': deadline
        })
        task = Task.objects.get(title='High Collateral Task')

        # Doer claims task (HIGH risk requires 25% collateral of 200 = 50 points)
        self.client.login(username='doer_user', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 150) # 200 - 50 = 150
        
        task.refresh_from_db()
        self.assertEqual(task.taker_collateral, 50)

    def test_dispute_creation_limits_for_high_risk_profiles(self):
        # Set doer to CRITICAL risk tier with 1 active dispute
        self.doer_profile.reputation_score = 30
        self.doer_profile.risk_tier = 'CRITICAL'
        self.doer_profile.save()

        # Create first task and dispute
        task1 = Task.objects.create(
            title='Task 1', description='Desc', reward=100,
            posted_by=self.poster, taken_by=self.doer, status='in_progress'
        )
        Dispute.objects.create(task=task1, raised_by=self.doer, reason='Reason 1', status='open')

        # Try to raise a second dispute on task2
        task2 = Task.objects.create(
            title='Task 2', description='Desc', reward=100,
            posted_by=self.poster, taken_by=self.doer, status='in_progress'
        )

        self.client.login(username='doer_user', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[task2.id]), {'reason': 'Reason 2'})

        # Should be blocked from raising second dispute
        self.assertFalse(Dispute.objects.filter(task=task2).exists())

    def test_profile_views_render_reputation_badges_and_summaries(self):
        self.client.login(username='doer_user', password='password123')

        # Own profile page
        res = self.client.get(reverse('profile'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Reputation & Risk Overview')
        self.assertContains(res, '100/1000')

        # Public user profile page
        res_public = self.client.get(reverse('user_profile', args=[self.poster.id]))
        self.assertEqual(res_public.status_code, 200)
        self.assertContains(res_public, 'Reputation & Trust Standing')

    def test_reputation_score_clamping_between_0_and_1000(self):
        profile = self.doer_profile
        profile.reputation_score = 995
        profile.save()

        # Increase by 20 -> should clamp to 1000
        ReputationService.update_reputation(profile, 20, "Bonus")
        profile.refresh_from_db()
        self.assertEqual(profile.reputation_score, 1000)

        # Decrease by 1100 -> should clamp to 0
        ReputationService.update_reputation(profile, -1100, "Penalty")
        profile.refresh_from_db()
        self.assertEqual(profile.reputation_score, 0)
