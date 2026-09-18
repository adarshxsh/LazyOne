from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, RewardLedger, ReputationLog
from basic.services.reputation import ReputationService
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

class ReputationEngineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 2000})
        self.poster_profile.rewards = 2000
        self.poster_profile.reputation_score = 75
        self.poster_profile.risk_tier = 'medium'
        self.poster_profile.save()

        self.doer = User.objects.create_user(username='doer', password='password123')
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1000})
        self.doer_profile.rewards = 1000
        self.doer_profile.reputation_score = 75
        self.doer_profile.risk_tier = 'medium'
        self.doer_profile.save()

        self.client = Client()

    def test_user_profile_default_reputation_fields(self):
        profile = self.doer.userprofile
        self.assertEqual(profile.reputation_score, 75)
        self.assertEqual(profile.tasks_completed, 0)
        self.assertEqual(profile.tasks_abandoned, 0)
        self.assertEqual(profile.disputes_raised, 0)
        self.assertEqual(profile.disputes_won, 0)
        self.assertEqual(profile.disputes_lost, 0)

    def test_complete_task_increments_completed_and_recalibrates_score(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        # Poster adds task (reward = 100)
        self.client.post(reverse('add_task'), {
            'title': 'Test Task',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Test Task')

        # Doer takes task
        self.client.login(username='doer', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Poster marks complete
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_completed, 1)
        self.assertEqual(self.doer_profile.reputation_score, 80) # 75 + 5 = 80
        self.assertEqual(self.doer_profile.risk_tier, 'low') # 80 = 'low' risk

    def test_abandon_task_increments_abandoned_and_deducts_reputation(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'Task To Abandon',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Task To Abandon')

        # Doer takes task
        self.client.login(username='doer', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Doer abandons task
        self.client.get(reverse('abandon_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.tasks_abandoned, 1)
        self.assertEqual(self.doer_profile.reputation_score, 60) # 75 - 15 = 60
        self.assertEqual(self.doer_profile.risk_tier, 'medium')

    def test_dispute_resolution_updates_won_and_lost_metrics(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'Disputed Task',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Disputed Task')

        # Doer takes task
        self.client.login(username='doer', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        # Doer raises dispute
        self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Payment delayed'})
        dispute = Dispute.objects.get(task=task)

        # Resolve dispute in favor of doer
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'doer'})

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.doer_profile.disputes_won, 1)
        self.assertEqual(self.doer_profile.reputation_score, 80) # 75 + 5

        self.assertEqual(self.poster_profile.disputes_lost, 1)
        self.assertEqual(self.poster_profile.reputation_score, 55) # 75 - 20

    def test_take_task_blocks_claim_if_reputation_score_exceeds_risk_threshold(self):
        # Set doer to critical risk tier
        self.doer_profile.reputation_score = 30
        self.doer_profile.risk_tier = 'critical'
        self.doer_profile.save()

        # Poster creates a high reward task (300 points > 200 max for critical risk)
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        
        self.client.post(reverse('add_task'), {
            'title': 'High Reward Task',
            'description': 'Description',
            'reward': '300',
            'deadline': deadline
        })
        task = Task.objects.get(title='High Reward Task')

        # Doer tries to take task
        self.client.login(username='doer', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

    def test_taker_dynamic_collateral_reservation_and_forfeiture_on_abandonment(self):
        # Set doer to high risk tier (requires 25% collateral)
        self.doer_profile.reputation_score = 45
        self.doer_profile.risk_tier = 'high'
        self.doer_profile.rewards = 100
        self.doer_profile.save()

        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Task with Collateral',
            'description': 'Description',
            'reward': '200',
            'deadline': deadline
        })
        task = Task.objects.get(title='Task with Collateral')

        # Doer takes task (requires 25% of 200 = 50 points collateral)
        self.client.login(username='doer', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))

        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 50) # 100 - 50 = 50
        
        task.refresh_from_db()
        self.assertEqual(task.taker_collateral, 50)

        # Doer abandons task -> collateral forfeited to poster
        self.client.get(reverse('abandon_task', args=[task.id]))

        self.poster_profile.refresh_from_db()
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='collateral_refund', amount=50).exists())

    def test_reputation_score_clamping_and_log(self):
        profile = self.doer_profile
        profile.reputation_score = 98
        profile.save()

        # Add 10 points -> should clamp to 100
        ReputationService.update_reputation(profile, 10, "Test Bonus")
        profile.refresh_from_db()
        self.assertEqual(profile.reputation_score, 100)

        log = ReputationLog.objects.filter(user=self.doer, reason="Test Bonus").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.change, 2) # Actual change was 2 (from 98 to 100)
        self.assertEqual(log.new_score, 100)
