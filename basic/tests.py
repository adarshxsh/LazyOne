from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.db import IntegrityError
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote


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


class DisputeVoteTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.worker = User.objects.create_user(username='worker', password='password123')
        self.worker_profile = UserProfile.objects.create(user=self.worker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        self.outsider = User.objects.create_user(username='outsider', password='password123')
        self.outsider_profile = UserProfile.objects.create(user=self.outsider, rewards=500)

        # Task
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task with dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.worker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Dispute raised by worker (deposit = 50 min)
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason="Unfair treatment",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )
        self.dispute.assigned_jurors.add(self.juror1, self.juror2, self.juror3)

    def test_dispute_vote_unique_constraint(self):
        vote1 = DisputeVote.objects.create(
            dispute=self.dispute,
            voter=self.juror1,
            vote_choice='worker',
            comment='Good worker'
        )
        self.assertIsNotNone(vote1.id)
        with self.assertRaises(IntegrityError):
            DisputeVote.objects.create(
                dispute=self.dispute,
                voter=self.juror1,
                vote_choice='poster',
                comment='Second vote attempt'
            )

    def test_dispute_detail_view_permissions(self):
        # Assigned juror can view
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_assigned_juror'])
        self.assertTrue(response.context['can_vote'])

        # Direct participant poster can view (cannot vote)
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_participant'])
        self.assertFalse(response.context['can_vote'])

        # Outsider user cannot view
        self.client.login(username='outsider', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_submit_vote_direct_participants_blocked(self):
        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'poster', 'comment': 'I should win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

        # Worker attempts to vote
        self.client.login(username='worker', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'worker', 'comment': 'I should win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

    def test_submit_vote_duplicate_blocked(self):
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'worker', 'comment': 'First vote'}
        )
        self.assertEqual(self.dispute.votes.count(), 1)

        # Attempt second vote
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'poster', 'comment': 'Changed mind'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 1)

    def test_inline_quorum_worker_victory(self):
        # 3 assigned jurors -> majority threshold is 2
        # Juror 1 votes for worker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'worker', 'comment': 'Worker did the job'}
        )
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

        # Juror 2 votes for worker -> hits majority (2/3)
        self.client.login(username='juror2', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'worker', 'comment': 'Agreed'}
        )
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.worker_profile.refresh_from_db()

        # Dispute and Task status updated
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'completed')

        # Worker rewards: initial 500 + 200 (task reward) + 50 (refunded deposit) = 750
        self.assertEqual(self.worker_profile.rewards, 750)

        # Check ledger
        completion_ledger = RewardLedger.objects.filter(user=self.worker, transaction_type='task_completion').first()
        self.assertIsNotNone(completion_ledger)
        self.assertEqual(completion_ledger.amount, 200)

        refund_ledger = RewardLedger.objects.filter(user=self.worker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 50)

    def test_inline_quorum_poster_victory(self):
        # Dispute raised by worker, deposit = 50 held from worker
        # 3 assigned jurors -> majority threshold is 2
        # Juror 1 votes for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'poster', 'comment': 'Poster is right'}
        )

        # Juror 2 votes for poster -> hits majority (2/3)
        self.client.login(username='juror2', password='password123')
        self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'poster', 'comment': 'Poster is right'}
        )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'forfeited')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster rewards: initial 1000 + 200 (task refund) + 50 (forfeited bond from worker) = 1250
        self.assertEqual(self.poster_profile.rewards, 1250)

        # Check ledger entries for poster
        cancel_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(cancel_ledger)
        self.assertEqual(cancel_ledger.amount, 200)

        award_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(award_ledger)
        self.assertEqual(award_ledger.amount, 50)

