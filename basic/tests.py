from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib import admin

from .models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


class StaffDisputeDashboardTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create users
        self.staff_user = User.objects.create_user(
            username='staff_mod', password='password123', is_staff=True
        )
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.regular_user = User.objects.create_user(username='regular_user', password='password123')
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=1000)

        # Create tasks & disputes
        deadline = timezone.now() + timedelta(days=1)
        self.task1 = Task.objects.create(
            title="Clean Room 101",
            description="Thorough cleaning required",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conv1 = Conversation.objects.create(task=self.task1)
        self.conv1.participants.add(self.poster, self.taker)

        self.dispute1 = Dispute.objects.create(
            task=self.task1,
            raised_by=self.taker,
            reason="Poster claims room was not cleaned properly",
            status='open'
        )

        self.task2 = Task.objects.create(
            title="Deliver Notes",
            description="Hand over lecture notes",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conv2 = Conversation.objects.create(task=self.task2)
        self.conv2.participants.add(self.poster, self.taker)

        self.dispute2 = Dispute.objects.create(
            task=self.task2,
            raised_by=self.taker,
            reason="Notes were delivered late",
            status='resolved'
        )

    def test_non_staff_access_dashboard_denied(self):
        self.client.login(username='regular_user', password='password123')
        response = self.client.get(reverse('staff_dispute_manage'), follow=True)
        self.assertRedirects(response, reverse('home'))
        messages = [m.message for m in response.context['messages']]
        self.assertTrue(any('not authorized' in m.lower() for m in messages))

    def test_unauthenticated_access_dashboard_redirects_login(self):
        response = self.client.get(reverse('staff_dispute_manage'))
        self.assertRedirects(response, '/login/?next=/disputes/manage/')

    def test_staff_access_dashboard_success(self):
        self.client.login(username='staff_mod', password='password123')
        response = self.client.get(reverse('staff_dispute_manage'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'disputes_manage.html')
        self.assertIn('disputes', response.context)

    def test_dashboard_filtering_by_status(self):
        self.client.login(username='staff_mod', password='password123')

        # Open disputes
        response_open = self.client.get(reverse('staff_dispute_manage') + '?status=open')
        disputes_open = list(response_open.context['disputes'])
        self.assertEqual(len(disputes_open), 1)
        self.assertEqual(disputes_open[0].id, self.dispute1.id)

        # Resolved disputes
        response_resolved = self.client.get(reverse('staff_dispute_manage') + '?status=resolved')
        disputes_resolved = list(response_resolved.context['disputes'])
        self.assertEqual(len(disputes_resolved), 1)
        self.assertEqual(disputes_resolved[0].id, self.dispute2.id)

        # All disputes
        response_all = self.client.get(reverse('staff_dispute_manage') + '?status=all')
        disputes_all = list(response_all.context['disputes'])
        self.assertEqual(len(disputes_all), 2)

    def test_dashboard_search_functionality(self):
        self.client.login(username='staff_mod', password='password123')

        # Search by task title
        response = self.client.get(reverse('staff_dispute_manage') + '?search=Clean')
        disputes = list(response.context['disputes'])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0].id, self.dispute1.id)

        # Search by username
        response_user = self.client.get(reverse('staff_dispute_manage') + '?search=poster_user')
        disputes_user = list(response_user.context['disputes'])
        self.assertEqual(len(disputes_user), 2)

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='regular_user', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])
        response = self.client.post(url, {'action': 'award_taker'}, follow=True)
        self.assertRedirects(response, reverse('home'))

        self.dispute1.refresh_from_db()
        self.assertEqual(self.dispute1.status, 'open')

    def test_resolve_dispute_award_taker(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        initial_taker_rewards = self.taker_profile.rewards
        response = self.client.post(url, {'action': 'award_taker', 'note': 'Well done'}, follow=True)
        
        self.assertRedirects(response, reverse('staff_dispute_manage'))

        # Check DB states
        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task1.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task1, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task1.reward)

        # Check Notifications
        notifications_poster = Notification.objects.filter(recipient=self.poster)
        notifications_taker = Notification.objects.filter(recipient=self.taker)
        self.assertTrue(notifications_poster.exists())
        self.assertTrue(notifications_taker.exists())

    def test_resolve_dispute_refund_poster(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        initial_poster_rewards = self.poster_profile.rewards
        response = self.client.post(url, {'action': 'refund_poster', 'note': 'Incomplete work'}, follow=True)

        self.assertRedirects(response, reverse('staff_dispute_manage'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task1.reward)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task1, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task1.reward)

        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_resolve_dispute_dismiss(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        response = self.client.post(url, {'action': 'dismiss'}, follow=True)

        self.assertRedirects(response, reverse('staff_dispute_manage'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'in_progress')

        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_admin_model_registrations(self):
        self.assertTrue(admin.site.is_registered(UserProfile))
        self.assertTrue(admin.site.is_registered(Task))
        self.assertTrue(admin.site.is_registered(RewardLedger))
        self.assertTrue(admin.site.is_registered(Dispute))

