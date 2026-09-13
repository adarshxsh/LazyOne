from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, Conversation

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
