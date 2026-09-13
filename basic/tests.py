from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation

class DjangoAdminRegistrationTest(TestCase):
    def test_domain_models_registered_in_admin(self):
        registered_models = admin.site._registry
        self.assertIn(Dispute, registered_models)
        self.assertIn(Task, registered_models)
        self.assertIn(UserProfile, registered_models)
        self.assertIn(RewardLedger, registered_models)

class StaffModerationDashboardAccessTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        UserProfile.objects.get_or_create(user=self.regular_user, defaults={'rewards': 500})
        UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 500})

        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker raised dispute for incomplete payment'
        )
        Conversation.objects.create(task=self.task)

    def test_anonymous_user_access_dashboard_denied(self):
        response = self.client.get(reverse('staff_dispute_dashboard'))
        self.assertRedirects(response, reverse('home'))

    def test_regular_user_access_dashboard_denied(self):
        self.client.login(username='regular', password='password123')
        response = self.client.get(reverse('staff_dispute_dashboard'))
        self.assertRedirects(response, reverse('home'))

    def test_staff_user_access_dashboard_success(self):
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('staff_dispute_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'moderation_dashboard.html')
        self.assertIn('disputes', response.context)
        self.assertEqual(len(response.context['disputes']), 1)

    def test_non_staff_resolution_attempt_denied(self):
        self.client.login(username='regular', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'ruling': 'poster'}
        )
        self.assertRedirects(response, reverse('home'))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

class StaffDisputeFilteringAndSearchTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(username='admin_staff', password='password123', is_staff=True)
        self.poster = User.objects.create_user(username='task_poster', password='password123')
        self.taker = User.objects.create_user(username='task_taker', password='password123')

        UserProfile.objects.get_or_create(user=self.staff)
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)

        self.task1 = Task.objects.create(
            title='Fix Plumbing Leak',
            description='Fix pipe under kitchen sink',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute_open = Dispute.objects.create(
            task=self.task1,
            raised_by=self.taker,
            reason='Plumbing work incomplete claim',
            status='open'
        )

        self.task2 = Task.objects.create(
            title='Design Website Logo',
            description='Create SVG logo',
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='completed'
        )
        self.dispute_resolved = Dispute.objects.create(
            task=self.task2,
            raised_by=self.poster,
            reason='Logo quality issue',
            status='resolved'
        )

        self.client.login(username='admin_staff', password='password123')

    def test_filter_by_status_open(self):
        response = self.client.get(reverse('staff_dispute_dashboard') + '?status=open')
        self.assertEqual(response.status_code, 200)
        disputes = list(response.context['disputes'])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0].id, self.dispute_open.id)

    def test_filter_by_status_resolved(self):
        response = self.client.get(reverse('staff_dispute_dashboard') + '?status=resolved')
        self.assertEqual(response.status_code, 200)
        disputes = list(response.context['disputes'])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0].id, self.dispute_resolved.id)

    def test_search_by_keyword(self):
        response = self.client.get(reverse('staff_dispute_dashboard') + '?q=Plumbing')
        self.assertEqual(response.status_code, 200)
        disputes = list(response.context['disputes'])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0].id, self.dispute_open.id)

class StaffDisputeResolutionRulingsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(username='mod_staff', password='password123', is_staff=True)
        self.poster = User.objects.create_user(username='alice_poster', password='password123')
        self.taker = User.objects.create_user(username='bob_taker', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        UserProfile.objects.get_or_create(user=self.staff)

        self.task = Task.objects.create(
            title='Yard Maintenance',
            description='Mow lawn and clear leaves',
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Disagreement on scope'
        )

    def test_resolve_favoring_poster(self):
        self.client.login(username='mod_staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'ruling': 'poster'}
        )
        self.assertRedirects(response, reverse('staff_dispute_dashboard'))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1150)

        # Verify RewardLedger transaction
        ledger_entry = RewardLedger.objects.get(user=self.poster, task=self.task)
        self.assertEqual(ledger_entry.amount, 150)
        self.assertEqual(ledger_entry.transaction_type, 'task_cancellation')

        # Verify Notifications for both parties
        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        taker_notif = Notification.objects.filter(recipient=self.taker).first()

        self.assertIsNotNone(poster_notif)
        self.assertIsNotNone(taker_notif)
        self.assertIn('resolved in your favor', poster_notif.message)
        self.assertIn('resolved in favor of the poster', taker_notif.message)

    def test_resolve_favoring_taker(self):
        self.client.login(username='mod_staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'ruling': 'taker'}
        )
        self.assertRedirects(response, reverse('staff_dispute_dashboard'))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 650)

        # Verify RewardLedger transaction
        ledger_entry = RewardLedger.objects.get(user=self.taker, task=self.task)
        self.assertEqual(ledger_entry.amount, 150)
        self.assertEqual(ledger_entry.transaction_type, 'task_completion')

        # Verify Notifications for both parties
        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        taker_notif = Notification.objects.filter(recipient=self.taker).first()

        self.assertIsNotNone(poster_notif)
        self.assertIsNotNone(taker_notif)
        self.assertIn('resolved in favor of the taker', poster_notif.message)
        self.assertIn('resolved in your favor', taker_notif.message)

    def test_resolve_already_resolved_dispute_prevented(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='mod_staff', password='password123')
        response = self.client.post(
            reverse('resolve_dispute', args=[self.dispute.id]),
            {'ruling': 'poster'}
        )
        self.assertRedirects(response, reverse('staff_dispute_dashboard'))

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertEqual(RewardLedger.objects.filter(task=self.task).count(), 0)
