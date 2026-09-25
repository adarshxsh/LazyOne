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


class DisputeVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Voting Task",
            description="Task for testing voting",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Raise dispute by taker
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work submitted but not accepted'}
        )
        self.dispute = Dispute.objects.get(task=self.task)

        # Create 5 community jurors
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=500)
            self.jurors.append(juror)

    def test_non_participant_can_view_open_dispute_detail(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voting Task")
        self.assertContains(response, "Juror Voting")

    def test_cast_vote_persists_vote_and_prevents_duplicate(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.poster.id, 'comment': 'Poster seems right'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.assertEqual(self.dispute.votes.count(), 1)
        vote = self.dispute.votes.first()
        self.assertEqual(vote.voter, self.jurors[0])
        self.assertEqual(vote.voted_for, self.poster)
        self.assertEqual(vote.comment, 'Poster seems right')

        # Duplicate vote attempt
        response2 = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.taker.id, 'comment': 'Changing my mind'}
        )
        self.assertEqual(self.dispute.votes.count(), 1)

    def test_participant_cannot_vote_on_own_dispute(self):
        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertEqual(self.dispute.votes.count(), 0)

        # Taker attempts to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertEqual(self.dispute.votes.count(), 0)

    def test_realtime_consensus_settlement_worker_wins(self):
        # Jurors 1, 2, 3 vote for Worker (taker)
        # Jurors 4, 5 vote for Poster (poster)
        vote_choices = [self.taker, self.taker, self.taker, self.poster, self.poster]

        for i, juror in enumerate(self.jurors):
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('vote_dispute', args=[self.dispute.id]),
                {'voted_for': vote_choices[i].id, 'comment': f'Vote by {juror.username}'}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'completed')

        # Taker starting rewards = 100, minus deposit 60 = 40.
        # Plus reward 300 + deposit refund 60 = 400.
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

    def test_realtime_consensus_settlement_poster_wins(self):
        # Jurors 1, 2, 3 vote for Poster
        # Jurors 4, 5 vote for Worker
        vote_choices = [self.poster, self.poster, self.poster, self.taker, self.taker]

        for i, juror in enumerate(self.jurors):
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('vote_dispute', args=[self.dispute.id]),
                {'voted_for': vote_choices[i].id, 'comment': f'Vote by {juror.username}'}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'forfeited')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster starting rewards = 1000.
        # Refund reward 300 + forfeited deposit 60 = 1360.
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1360)


