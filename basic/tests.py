from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, RewardLedger, ReputationLog
from basic.services.reputation import ReputationService
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

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
