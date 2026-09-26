from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment, DisputeEvidence


class JurorPrivateDeliberationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror = User.objects.create_user(username='juror1', password='password123')
        self.juror_profile = UserProfile.objects.create(user=self.juror, rewards=1000)

        self.outsider = User.objects.create_user(username='outsider', password='password123')
        self.outsider_profile = UserProfile.objects.create(user=self.outsider, rewards=1000)

        # Task and Task Conversation
        self.task = Task.objects.create(
            title="Disputed Delivery Task",
            description="Task for testing dispute deliberation",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.task_conv = Conversation.objects.create(
            task=self.task,
            conversation_type='task'
        )
        self.task_conv.participants.add(self.poster, self.taker)

        # Dispute and Juror Assignment
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Item was damaged upon delivery",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror)

    def test_impaneled_juror_can_view_dispute_detail(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disputed Delivery Task")
        self.assertContains(response, "Item was damaged upon delivery")
        self.assertContains(response, "Task Chat History")
        self.assertContains(response, "Private Deliberation Room")

    def test_unauthorized_user_blocked_from_dispute_detail(self):
        self.client.login(username='outsider', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_juror_read_only_access_to_task_chat(self):
        # View task chat
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.task_conv.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get('is_read_only'))

        # Juror attempting to post message in task chat gets HTTP 403 Forbidden
        send_resp = self.client.post(
            reverse('send_message', args=[self.task_conv.id]),
            {'content': 'Juror trying to send message to task chat'}
        )
        self.assertEqual(send_resp.status_code, 403)

    def test_task_participants_can_submit_evidence(self):
        self.client.login(username='taker', password='password123')
        resp = self.client.post(
            reverse('submit_evidence', args=[self.dispute.id]),
            {
                'description': 'Photos showing package damage on arrival',
                'evidence_url': 'https://example.com/proof.jpg'
            }
        )
        self.assertRedirects(resp, reverse('dispute_detail', args=[self.dispute.id]))

        evidence = DisputeEvidence.objects.filter(dispute=self.dispute).first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.submitted_by, self.taker)
        self.assertEqual(evidence.description, 'Photos showing package damage on arrival')
        self.assertEqual(evidence.evidence_url, 'https://example.com/proof.jpg')

        # Verify evidence displays on dispute detail page
        detail_resp = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertContains(detail_resp, 'Photos showing package damage on arrival')

    def test_unauthorized_user_cannot_submit_evidence(self):
        self.client.login(username='juror1', password='password123')
        resp = self.client.post(
            reverse('submit_evidence', args=[self.dispute.id]),
            {'description': 'Juror statement'}
        )
        self.assertRedirects(resp, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeEvidence.objects.filter(dispute=self.dispute).exists())

    def test_juror_private_deliberation_chat(self):
        delib_conv = self.dispute.deliberation_conversation
        self.assertEqual(delib_conv.conversation_type, 'deliberation')

        # Impaneled juror can view deliberation chat
        self.client.login(username='juror1', password='password123')
        view_resp = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(view_resp.status_code, 200)

        # Impaneled juror can post deliberation message
        post_resp = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'I agree the package was damaged prior to handover.'}
        )
        self.assertEqual(post_resp.status_code, 200)

    def test_task_participants_blocked_from_deliberation_chat(self):
        delib_conv = self.dispute.deliberation_conversation

        # Task poster blocked from viewing deliberation chat
        self.client.login(username='poster', password='password123')
        poster_view = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(poster_view.status_code, 403)

        # Task poster blocked from sending deliberation message
        poster_post = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'Poster trying to join deliberation'}
        )
        self.assertEqual(poster_post.status_code, 403)

        # Task taker blocked from viewing deliberation chat
        self.client.login(username='taker', password='password123')
        taker_view = self.client.get(reverse('chat_view', args=[delib_conv.id]))
        self.assertEqual(taker_view.status_code, 403)

        # Task taker blocked from sending deliberation message
        taker_post = self.client.post(
            reverse('send_message', args=[delib_conv.id]),
            {'content': 'Taker trying to join deliberation'}
        )
        self.assertEqual(taker_post.status_code, 403)



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
        self.assertEqual(dispute.status, 'open')
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
        self.assertEqual(dispute.status, 'resolved')

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

