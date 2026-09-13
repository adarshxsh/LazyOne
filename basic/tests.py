from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, Notification, Conversation


class DisputeStateMachineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=1000)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)
        self.client = Client()

    def test_status_choices_and_initialization(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work not accepted",
            status=Dispute.STATUS_INITIATED
        )
        self.assertEqual(dispute.status, Dispute.STATUS_INITIATED)
        self.assertIn(dispute.status, [c[0] for c in Dispute.STATUS_CHOICES])

    def test_valid_state_transitions_and_deadlines(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work incomplete",
            status=Dispute.STATUS_INITIATED
        )
        
        # Transition to evidence_submission
        dispute.transition_to(Dispute.STATUS_EVIDENCE_SUBMISSION)
        self.assertEqual(dispute.status, Dispute.STATUS_EVIDENCE_SUBMISSION)
        self.assertIsNotNone(dispute.evidence_deadline)

        # Transition to voting
        dispute.transition_to(Dispute.STATUS_VOTING)
        self.assertEqual(dispute.status, Dispute.STATUS_VOTING)
        self.assertIsNotNone(dispute.voting_deadline)

        # Transition to appealed
        dispute.transition_to(Dispute.STATUS_APPEALED)
        self.assertEqual(dispute.status, Dispute.STATUS_APPEALED)
        self.assertIsNotNone(dispute.appeal_deadline)

        # Transition back to voting (appeal granted)
        dispute.transition_to(Dispute.STATUS_VOTING)
        self.assertEqual(dispute.status, Dispute.STATUS_VOTING)

        # Transition to resolved
        dispute.transition_to(Dispute.STATUS_RESOLVED)
        self.assertEqual(dispute.status, Dispute.STATUS_RESOLVED)

    def test_invalid_state_transitions_raise_validation_error(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Invalid transition test",
            status=Dispute.STATUS_INITIATED
        )
        # Cannot jump from initiated to resolved
        with self.assertRaises(ValidationError):
            dispute.transition_to(Dispute.STATUS_RESOLVED)

        # Cannot jump from initiated to voting
        with self.assertRaises(ValidationError):
            dispute.transition_to(Dispute.STATUS_VOTING)

        dispute.transition_to(Dispute.STATUS_EVIDENCE_SUBMISSION)

        # Cannot jump from evidence_submission to appealed
        with self.assertRaises(ValidationError):
            dispute.transition_to(Dispute.STATUS_APPEALED)

        dispute.transition_to(Dispute.STATUS_VOTING)

        # Direct backward transition from voting to evidence_submission is prohibited
        with self.assertRaises(ValidationError):
            dispute.transition_to(Dispute.STATUS_EVIDENCE_SUBMISSION)

        dispute.transition_to(Dispute.STATUS_CANCELLED)

        # Cancelled is terminal
        with self.assertRaises(ValidationError):
            dispute.transition_to(Dispute.STATUS_VOTING)

    def test_direct_assignment_validation(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Direct assignment test",
            status=Dispute.STATUS_INITIATED
        )
        dispute.status = Dispute.STATUS_RESOLVED
        with self.assertRaises(ValidationError):
            dispute.save()

    def test_raise_dispute_handler(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Payment delayed'})
        self.task.refresh_from_db()
        dispute = Dispute.objects.get(task=self.task)
        
        self.assertEqual(dispute.status, Dispute.STATUS_EVIDENCE_SUBMISSION)
        self.assertIsNotNone(dispute.evidence_deadline)
        self.assertEqual(self.task.status, 'disputed')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_withdraw_dispute_handler(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Payment delayed'})
        dispute = Dispute.objects.get(task=self.task)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, Dispute.STATUS_CANCELLED)
        self.assertEqual(self.task.status, 'in_progress')
        self.assertRedirects(response, reverse('my_tasks'))

    def test_complete_task_resolves_dispute(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Misunderstanding'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        
        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, Dispute.STATUS_RESOLVED)
        self.assertEqual(self.task.status, 'completed')

    def test_transition_dispute_view(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Need arbitration'})
        dispute = Dispute.objects.get(task=self.task)

        # Transition to voting from UI
        response = self.client.post(
            reverse('transition_dispute', args=[dispute.id]),
            {'target_status': 'voting'}
        )
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.STATUS_VOTING)
        self.assertIsNotNone(dispute.voting_deadline)

        # Attempt invalid transition from UI (e.g., voting -> evidence_submission)
        response = self.client.post(
            reverse('transition_dispute', args=[dispute.id]),
            {'target_status': 'evidence_submission'},
            follow=True
        )
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.STATUS_VOTING) # Status unchanged
        self.assertContains(response, "Cannot transition dispute from &#x27;voting&#x27; to &#x27;evidence_submission&#x27;")

    def test_dispute_detail_ui_rendering(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'UI test reason'})
        dispute = Dispute.objects.get(task=self.task)

        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Evidence Submission")
        self.assertContains(response, "Dispute Lifecycle Timeline")
        self.assertContains(response, "Proceed to Voting Phase")
        self.assertContains(response, "Evidence Deadline")
