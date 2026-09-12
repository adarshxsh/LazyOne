from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

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
        # 100 - (1 * 20) = 80, but since base is recalculate:
        # recalculate_reputation_and_risk uses base 100 - (1 * 20) = 80
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
