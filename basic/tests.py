from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.db import IntegrityError
from django.core.management import call_command
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


class DisputeVoteAndConsensusTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.task = Task.objects.create(
            title="Voting Task",
            description="Task to test peer consensus voting",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=timezone.now() + timedelta(days=3)
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work delivered but rejected without feedback",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        # Community voters
        self.voter1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.voter1, rewards=500)

        self.voter2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.voter2, rewards=500)

        self.voter3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.voter3, rewards=500)

    def test_dispute_vote_model_unique_constraint(self):
        DisputeVote.objects.create(
            dispute=self.dispute,
            voter=self.voter1,
            voted_for=self.poster
        )
        with self.assertRaises(IntegrityError):
            DisputeVote.objects.create(
                dispute=self.dispute,
                voter=self.voter1,
                voted_for=self.taker
            )

    def test_dispute_detail_view_community_authorization(self):
        # Community user (voter1) can view open dispute
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voting Task")
        self.assertContains(response, "Community Jury Tally & Quorum")

    def test_participants_cannot_vote(self):
        # Task taker attempts to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Task poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for_id': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_eligible_community_user_can_vote(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)
        vote = DisputeVote.objects.first()
        self.assertEqual(vote.voter, self.voter1)
        self.assertEqual(vote.voted_for, self.taker)

    def test_duplicate_vote_prevention(self):
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id}
        )
        # Second attempt
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for_id': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

    def test_consensus_majority_tally_taker_wins(self):
        # Taker raised dispute. Quorum = 3.
        # Voter 1 votes Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.taker.id})

        # Voter 2 votes Taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.taker.id})

        # Dispute should still be open (2 votes < quorum 3)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # Voter 3 votes Poster
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.poster.id})

        # Quorum met: 2 for Taker, 1 for Poster -> Taker wins!
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker raised dispute: Taker gets task reward (200) + deposit refund (50) = 200 + 50 = 250 added to initial 200 -> 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        # Deposit status refunded
        self.assertEqual(self.dispute.escrow_status, 'refunded')

    def test_consensus_majority_tally_poster_wins(self):
        # Taker raised dispute. Quorum = 3.
        # Voter 1 votes Poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.poster.id})

        # Voter 2 votes Poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.poster.id})

        # Voter 3 votes Taker
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for_id': self.taker.id})

        # Quorum met: 2 for Poster, 1 for Taker -> Poster wins!
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Taker raised dispute and lost: Taker's deposit (50) forfeited to Poster.
        # Poster gets task reward refund (200) + forfeited bond (50) = 250 added to initial 1000 -> 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

        # Deposit status forfeited
        self.assertEqual(self.dispute.escrow_status, 'forfeited')

    def test_resolve_expired_disputes_command_evaluates_consensus(self):
        # Create 3 votes (2 for Poster, 1 for Taker) manually
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter1, voted_for=self.poster)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter2, voted_for=self.poster)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter3, voted_for=self.taker)

        # Run management command
        call_command('resolve_expired_disputes')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')
