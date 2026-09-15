from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile

class DisputeLifecycleTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1000})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Task dispute reason',
            status='open'
        )
        self.client = Client()

    def test_dispute_status_choices(self):
        statuses = dict(Dispute.STATUS_CHOICES)
        expected_keys = [
            'open', 'evidence_submission', 'jury_selection',
            'voting', 'appealed', 'resolved', 'withdrawn'
        ]
        for key in expected_keys:
            self.assertIn(key, statuses)

    def test_valid_transitions(self):
        # open -> evidence_submission
        self.assertTrue(self.dispute.can_transition_to('evidence_submission'))
        self.dispute.transition_to('evidence_submission', save=True)
        self.assertEqual(self.dispute.status, 'evidence_submission')

        # evidence_submission -> jury_selection
        self.assertTrue(self.dispute.can_transition_to('jury_selection'))
        self.dispute.transition_to('jury_selection', save=True)
        self.assertEqual(self.dispute.status, 'jury_selection')

        # jury_selection -> voting
        self.assertTrue(self.dispute.can_transition_to('voting'))
        self.dispute.transition_to('voting', save=True)
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> appealed
        self.assertTrue(self.dispute.can_transition_to('appealed'))
        self.dispute.transition_to('appealed', save=True)
        self.assertEqual(self.dispute.status, 'appealed')

        # appealed -> voting
        self.assertTrue(self.dispute.can_transition_to('voting'))
        self.dispute.transition_to('voting', save=True)
        self.assertEqual(self.dispute.status, 'voting')

        # voting -> resolved
        self.assertTrue(self.dispute.can_transition_to('resolved'))
        self.dispute.transition_to('resolved', save=True)
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_transition_open_to_resolved_raises_validation_error(self):
        self.assertFalse(self.dispute.can_transition_to('resolved'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('resolved')

    def test_invalid_transition_from_terminal_states(self):
        self.dispute.status = 'resolved'
        self.dispute.save()
        self.assertFalse(self.dispute.can_transition_to('open'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('open')

        self.dispute.status = 'withdrawn'
        self.dispute.save()
        self.assertFalse(self.dispute.can_transition_to('open'))
        with self.assertRaises(ValidationError):
            self.dispute.transition_to('open')

    def test_withdraw_dispute_view(self):
        self.client.login(username='doer', password='password123')
        url = reverse('withdraw_dispute', args=[self.dispute.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')

    def test_complete_task_with_active_open_dispute_fails(self):
        self.client.login(username='poster', password='password123')
        url = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

    def test_complete_task_with_resolvable_dispute_succeeds(self):
        self.dispute.status = 'voting'
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        url = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

    def test_dispute_detail_view_rendering(self):
        self.client.login(username='poster', password='password123')
        url = reverse('dispute_detail', args=[self.dispute.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Open')
        self.assertContains(response, 'Move to Evidence Submission')

    def test_transition_dispute_view(self):
        self.client.login(username='poster', password='password123')
        url = reverse('transition_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'target_state': 'evidence_submission'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'evidence_submission')
