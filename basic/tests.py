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


class OpenCommunityJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=500)

        # Task & Conversation
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Community Dispute Task",
            description="Task Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Dispute (raised by taker, deposit bond 60)
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but not acknowledged",
            deposit_amount=60,
            escrow_status='held',
            status='open'
        )

    def test_view_authorization_authenticated_non_participant(self):
        # Authenticated non-participant can view dispute detail page
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Community Dispute Task")
        self.assertContains(response, "Community Jury Voting Progress")

    def test_participant_voting_blocked(self):
        # Task poster cannot vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

        # Task taker cannot vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

    def test_non_participant_voting_and_uniqueness(self):
        # Juror1 votes for taker
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.taker.id, 'feedback': 'Work looks done'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 1)
        vote = self.dispute.votes.first()
        self.assertEqual(vote.voter, self.juror1)
        self.assertEqual(vote.voted_for, self.taker)

        # Juror1 tries to vote again -> blocked by unique constraint
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 1)

    def test_consensus_resolution_favor_poster(self):
        # Juror1 votes for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.poster.id})

        # Juror2 votes for taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})

        # Dispute remains open at 2 votes
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # Juror3 votes for poster (3rd vote -> 2/3 for poster > 50%)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.poster.id})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        # Poster refunded reward 300: 1000 + 300 = 1300
        self.assertEqual(self.poster_profile.rewards, 1300)
        # Taker deposit bond forfeited
        self.assertEqual(self.dispute.escrow_status, 'forfeited')

        # Check ledger
        poster_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, 300)

    def test_consensus_resolution_favor_taker(self):
        # 3 jurors vote for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        # Taker awarded task reward (300) + deposit bond refund (60): 200 + 300 + 60 = 560
        self.assertEqual(self.taker_profile.rewards, 560)
        self.assertEqual(self.dispute.escrow_status, 'refunded')

        # Check ledger
        completion_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion').first()
        self.assertIsNotNone(completion_ledger)
        self.assertEqual(completion_ledger.amount, 300)

    def test_closed_dispute_voting_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

    def test_chat_view_read_only_access(self):
        # Non-participant juror can view disputed chat in read-only mode
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])
        self.assertContains(response, "read-only mode")


