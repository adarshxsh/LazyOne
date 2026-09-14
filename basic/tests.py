from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, Notification, RewardLedger, Conversation


class DisputeStateMachineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

    def test_raise_dispute_initializes_evidence_pending(self):
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work completed but poster unpaid.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = self.task.dispute
        self.assertEqual(dispute.status, 'evidence_pending')
        self.assertEqual(dispute.raised_by, self.taker)
        # Notification dispatched to poster
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_submit_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Taker submits evidence
        url = reverse('submit_evidence', args=[dispute.id])
        resp = self.client_taker.post(url, {
            'description': 'Submitted completed work screenshot',
            'attachment_link': 'https://example.com/proof.png'
        })
        self.assertRedirects(resp, reverse('dispute_detail', args=[dispute.id]))

        evidence = DisputeEvidence.objects.filter(dispute=dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.submitted_by, self.taker)
        self.assertEqual(evidence.description, 'Submitted completed work screenshot')
        self.assertEqual(evidence.attachment_link, 'https://example.com/proof.png')

        # Notification created for counterparty (poster)
        self.assertTrue(Notification.objects.filter(recipient=self.poster, message__icontains='New evidence submitted').exists())

        # Unauthorized user cannot submit evidence
        resp_other = self.client_other.post(url, {'description': 'Hacker evidence'})
        self.assertRedirects(resp_other, reverse('home'))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 1)

    def test_transition_guards_and_withdrawal_restriction(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Direct transition from evidence_pending to resolved without mutual consent fails guard
        self.assertFalse(dispute.can_transition_to('resolved', is_mutual=False))
        with self.assertRaises(ValueError):
            dispute.transition_to('resolved', is_mutual=False)

        # Transition to under_review
        resp_review = self.client_taker.post(reverse('submit_for_review', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'under_review')

        # Withdrawal should now fail since status is under_review
        resp_withdraw = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'under_review') # Status unchanged

    def test_valid_withdrawal_during_evidence_pending(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        self.task.status = 'disputed'
        self.task.save()

        resp_withdraw = self.client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'withdrawn')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

    def test_auto_48h_transition(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        # Backdate dispute created_at to 49 hours ago
        Dispute.objects.filter(id=dispute.id).update(created_at=timezone.now() - timedelta(hours=49))
        dispute.refresh_from_db()

        self.assertTrue(dispute.check_auto_transition())
        self.assertEqual(dispute.status, 'under_review')
        self.assertTrue(Notification.objects.filter(recipient=self.poster, message__icontains='auto-transitioned').exists())

    def test_resolution_awards_rewards_and_creates_ledger(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='under_review'
        )
        self.task.status = 'disputed'
        self.task.save()

        resp_resolve = self.client_poster.post(reverse('resolve_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertIsNotNone(dispute.resolved_at)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700) # 500 + 200

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').exists())

    def test_complete_task_endpoint_resolves_active_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        self.task.status = 'disputed'
        self.task.save()

        resp_complete = self.client_poster.post(reverse('complete_task', args=[self.task.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

    def test_cannot_submit_evidence_when_under_review(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='under_review'
        )
        self.task.status = 'disputed'
        self.task.save()

        url = reverse('submit_evidence', args=[dispute.id])
        resp = self.client_taker.post(url, {'description': 'Late evidence'})
        self.assertRedirects(resp, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeEvidence.objects.filter(dispute=dispute).count(), 0)

    def test_poster_cannot_withdraw_taker_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Dispute reason', status='evidence_pending'
        )
        self.task.status = 'disputed'
        self.task.save()

        resp_withdraw = self.client_poster.post(reverse('withdraw_dispute', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'evidence_pending')

