from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CommunityJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        # Create Task
        self.task = Task.objects.create(
            title="Design Logo Task",
            description="Create a modern company logo",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        # Create Dispute raised by Taker
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Poster did not accept completed logo work",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        # Create 5 neutral peer jurors
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=500, reputation_score=100)
            self.jurors.append(juror)

    def test_neutral_member_can_view_open_dispute(self):
        self.client.login(username='juror_1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Design Logo Task")
        self.assertContains(response, "Community Jury Voting & Consensus")

    def test_direct_participants_cannot_vote(self):
        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote': 'poster'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Taker attempts to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote': 'taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_neutral_juror_can_vote_and_duplicate_vote_prevented(self):
        self.client.login(username='juror_1', password='password123')

        # First vote
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote': 'taker'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

        vote = DisputeVote.objects.first()
        self.assertEqual(vote.voter, self.jurors[0])
        self.assertEqual(vote.vote, 'taker')

        # Duplicate vote attempt
        response2 = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote': 'poster'}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

    def test_quorum_reached_triggers_settlement_and_escrow_and_juror_rewards(self):
        # 3 jurors vote for taker (worker), 2 jurors vote for poster
        votes = [
            ('juror_1', 'taker'),
            ('juror_2', 'taker'),
            ('juror_3', 'poster'),
            ('juror_4', 'poster'),
            ('juror_5', 'taker'),  # 5th vote reaches quorum!
        ]

        for username, choice in votes:
            self.client.login(username=username, password='password123')
            self.client.post(
                reverse('vote_dispute', args=[self.dispute.id]),
                {'vote': choice}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # 3-2 in favor of taker -> Taker wins!
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker receives task reward (200) + deposit refund (50) -> 200 + 200 + 50 = 450 (taker initial 200)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        # Check juror rewards (500 initial + 10 reward = 510 each)
        for juror in self.jurors:
            juror_profile = UserProfile.objects.get(user=juror)
            self.assertEqual(juror_profile.rewards, 510)

            # Ledger entry
            ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_reward').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, 10)

    def test_tie_vote_falls_back_to_staff_review(self):
        # 2 jurors vote for poster, 2 jurors vote for taker
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[0], vote='poster')
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[1], vote='poster')
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[2], vote='taker')
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[3], vote='taker')

        # Tally on tie
        self.dispute.tally_and_settle()
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.status, 'staff_review')

    def test_poster_wins_quorum_cancels_task_and_refunds_poster(self):
        # 3 jurors vote for poster, 2 for taker
        votes = [
            ('juror_1', 'poster'),
            ('juror_2', 'poster'),
            ('juror_3', 'taker'),
            ('juror_4', 'poster'),  # 3 poster
            ('juror_5', 'taker'),  # 5th vote reaches quorum
        ]

        for username, choice in votes:
            self.client.login(username=username, password='password123')
            self.client.post(
                reverse('vote_dispute', args=[self.dispute.id]),
                {'vote': choice}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Poster wins -> Task cancelled
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster initial rewards 1000 + 200 task refund + 50 forfeited deposit from taker = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

    def test_low_reputation_user_cannot_vote(self):
        low_rep_user = User.objects.create_user(username='low_rep', password='password123')
        UserProfile.objects.create(user=low_rep_user, rewards=500, reputation_score=30)

        self.client.login(username='low_rep', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote': 'poster'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_expired_dispute_voting_window_auto_tallies(self):
        # Cast 2 votes for taker and 1 for poster (3 votes total < quorum 5)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[0], vote='taker')
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[1], vote='taker')
        DisputeVote.objects.create(dispute=self.dispute, voter=self.jurors[2], vote='poster')

        # Fast forward time past 48 hours
        self.dispute.created_at = timezone.now() - timedelta(hours=49)
        self.dispute.save()

        # Accessing dispute detail triggers auto-settlement for expired voting window
        self.client.login(username='juror_4', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')



