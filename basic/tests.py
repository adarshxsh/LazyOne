from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, Notification, RewardLedger, Conversation


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Create task: reward = 300, 20% = 60 (> 50 minimum)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create small reward task: reward = 100, 20% = 20 (min 50 applies)
        self.small_task = Task.objects.create(
            title="Small Task",
            description="Small Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.small_task)

    def test_deposit_bond_calculation(self):
        # 20% of 300 = 60 (> 50)
        self.assertEqual(self.task.deposit_bond_amount, 60)
        # 20% of 100 = 20 (< 50, so minimum 50 applies)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
        # Set taker rewards to 30 (less than 60 required)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balance should remain unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'evidence_pending')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        # First raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'withdrawn')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 40 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


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
