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


class CommunityJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Community jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=1000)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=1000)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=1000)

        # Create task and conversation
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Community Dispute Task",
            description="Task with dispute for community jury",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Poster did not confirm work",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

    def test_dispute_vote_model_and_unique_constraint(self):
        vote = DisputeVote.objects.create(
            dispute=self.dispute,
            voter=self.juror1,
            voted_for=self.taker
        )
        self.assertEqual(vote.vote, 'taker')
        self.assertEqual(vote.choice, 'taker')
        self.assertEqual(self.dispute.votes.count(), 1)

        # Enforce unique constraint
        with self.assertRaises(IntegrityError):
            DisputeVote.objects.create(
                dispute=self.dispute,
                voter=self.juror1,
                voted_for=self.poster
            )

    def test_authenticated_community_member_can_view_open_dispute(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dispute_detail.html')
        self.assertContains(response, "Community Dispute Task")
        self.assertContains(response, "Community Jury Voting Tally")

    def test_direct_task_participants_cannot_vote(self):
        # Poster attempts to vote
        self.client.login(username='poster_user', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': 'poster'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Taker attempts to vote
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': 'taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_community_juror_voting_and_single_vote_enforcement(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': 'taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

        # Attempt to vote a second time
        response2 = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': 'poster'}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

    def test_automated_consensus_resolution_taker_win(self):
        # 3 jurors vote for taker -> consensus reached -> dispute resolved in favor of taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'taker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'taker'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker profile gets reward (500 + 200 = 700) + deposit refund (50) = 750
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 750)

    def test_automated_consensus_resolution_poster_win(self):
        # 3 jurors vote for poster -> dispute resolved in favor of poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'poster'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'poster'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': 'poster'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster profile gets task reward refunded (1000 + 200 = 1200) + deposit forfeited by taker (50) = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

    def test_consensus_resolution_on_expired_deadline(self):
        # 1 vote for taker, 0 for poster
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, voted_for=self.taker)

        # Fast forward creation date past 72h
        self.dispute.created_at = timezone.now() - timedelta(hours=73)
        self.dispute.save()

        # Check consensus
        resolved = self.dispute.check_and_resolve_consensus()
        self.assertTrue(resolved)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')


