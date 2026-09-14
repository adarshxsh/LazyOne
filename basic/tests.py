from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, Conversation

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
