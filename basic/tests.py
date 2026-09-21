from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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


from django.db import IntegrityError
from django.core.management import call_command
from basic.models import DisputeVote


class CommunityDisputeVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Disputed task
        self.task = Task.objects.create(
            title="Peer Voting Task",
            description="Task undergoing dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work submitted but poster refused payment",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        # Create potential community voters
        self.voters = []
        for i in range(1, 7):
            voter = User.objects.create_user(username=f'voter{i}', password='password123')
            UserProfile.objects.create(user=voter, rewards=100)
            self.voters.append(voter)

    def test_dispute_vote_model_and_unique_constraint(self):
        vote1 = DisputeVote.objects.create(
            dispute=self.dispute,
            voter=self.voters[0],
            vote_choice='taken_by',
            reason='Taker provided proof'
        )
        self.assertEqual(vote1.vote_choice, 'taken_by')
        self.assertEqual(vote1.reason, 'Taker provided proof')

        # Duplicate vote should raise IntegrityError
        with self.assertRaises(IntegrityError):
            DisputeVote.objects.create(
                dispute=self.dispute,
                voter=self.voters[0],
                vote_choice='posted_by',
                reason='Changed mind'
            )

    def test_public_disputes_list_view(self):
        # Unauthenticated redirect
        response = self.client.get(reverse('public_disputes_list'))
        self.assertRedirects(response, '/login/?next=/disputes/')

        # Authenticated view
        self.client.login(username='voter1', password='password123')
        response = self.client.get(reverse('public_disputes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Peer Voting Task')

    def test_non_participant_can_view_dispute_detail(self):
        self.client.login(username='voter1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dispute Details')
        self.assertContains(response, 'Cast Your Jury Vote')

    def test_task_participants_blocked_from_voting(self):
        # Task Poster attempt
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'posted_by', 'reason': 'I am the poster'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Task Taker attempt
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'taken_by', 'reason': 'I am the taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_voter_eligibility(self):
        # Ineligible voter (rewards < 50, not verified)
        low_user = User.objects.create_user(username='low_user', password='password123')
        UserProfile.objects.create(user=low_user, rewards=10)

        self.client.login(username='low_user', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'taken_by', 'reason': 'Eligible?'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=low_user).exists())

        # Verified low points user
        verified_user = User.objects.create_user(username='verified_user', password='password123')
        UserProfile.objects.create(user=verified_user, rewards=10, is_phone_verified=True)

        self.client.login(username='verified_user', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'taken_by', 'reason': 'Verified user'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertTrue(DisputeVote.objects.filter(dispute=self.dispute, voter=verified_user).exists())

    def test_submit_valid_vote_and_prevent_double_voting(self):
        self.client.login(username='voter1', password='password123')
        
        # First vote
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'taken_by', 'reason': 'Looks genuine'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        vote = DisputeVote.objects.get(dispute=self.dispute, voter=self.voters[0])
        self.assertEqual(vote.vote_choice, 'taken_by')
        self.assertEqual(vote.reason, 'Looks genuine')

        # Attempt double vote
        response2 = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'posted_by', 'reason': 'Changed mind'}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        
        # Existing vote preserved
        vote.refresh_from_db()
        self.assertEqual(vote.vote_choice, 'taken_by')
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voters[0]).count(), 1)

    def test_consensus_resolution_on_quorum(self):
        # 4 votes cast: 3 for taker, 1 for poster
        choices = ['taken_by', 'taken_by', 'posted_by', 'taken_by']
        for idx in range(4):
            DisputeVote.objects.create(
                dispute=self.dispute,
                voter=self.voters[idx],
                vote_choice=choices[idx],
                reason=f"Vote {idx}"
            )

        # Dispute should still be open
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # 5th vote cast via endpoint (quorum met)
        self.client.login(username='voter5', password='password123')
        self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': 'taken_by', 'reason': '5th vote'}
        )

        # Dispute should be resolved
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Task should be completed (taker won 4 to 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker profile: initial (100) + reward (200) + deposit refund (50) = 350
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 350)

        # Check voter rewards (10 points each)
        for voter in self.voters[:5]:
            v_profile = voter.userprofile
            v_profile.refresh_from_db()
            self.assertEqual(v_profile.rewards, 110)
            self.assertTrue(RewardLedger.objects.filter(user=voter, transaction_type='voter_reward').exists())

    def test_resolve_expired_disputes_command_fallback_and_consensus(self):
        # Dispute 1: Expired with < 3 votes -> remains open for staff arbitration
        old_time = timezone.now() - timedelta(days=10)
        task_old = Task.objects.create(
            title="Old Task 1", description="D", reward=100,
            posted_by=self.poster, taken_by=self.taker, status='disputed'
        )
        dispute_old = Dispute.objects.create(
            task=task_old, raised_by=self.taker, reason="D", deposit_amount=50, status='open'
        )
        dispute_old.created_at = old_time
        dispute_old.save()

        # Cast 2 votes
        DisputeVote.objects.create(dispute=dispute_old, voter=self.voters[0], vote_choice='taken_by')
        DisputeVote.objects.create(dispute=dispute_old, voter=self.voters[1], vote_choice='taken_by')

        # Dispute 2: Expired with 3 votes (2 poster, 1 taker) -> poster wins
        task_old2 = Task.objects.create(
            title="Old Task 2", description="D", reward=100,
            posted_by=self.poster, taken_by=self.taker, status='disputed'
        )
        dispute_old2 = Dispute.objects.create(
            task=task_old2, raised_by=self.taker, reason="D", deposit_amount=50, status='open'
        )
        dispute_old2.created_at = old_time
        dispute_old2.save()

        DisputeVote.objects.create(dispute=dispute_old2, voter=self.voters[0], vote_choice='posted_by')
        DisputeVote.objects.create(dispute=dispute_old2, voter=self.voters[1], vote_choice='posted_by')
        DisputeVote.objects.create(dispute=dispute_old2, voter=self.voters[2], vote_choice='taken_by')

        # Run command
        call_command('resolve_expired_disputes', days=7)

        # Dispute 1 (< 3 votes) remains open
        dispute_old.refresh_from_db()
        self.assertEqual(dispute_old.status, 'open')

        # Dispute 2 (>= 3 votes) resolved
        dispute_old2.refresh_from_db()
        self.assertEqual(dispute_old2.status, 'resolved')
        task_old2.refresh_from_db()
        self.assertEqual(task_old2.status, 'cancelled')


