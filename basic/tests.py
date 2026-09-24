from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
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


class DisputeJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create task & open dispute
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Jury Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Dispute with deposit bond = 50
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair requirements",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        # Create 5 community members (jurors)
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=100)
            self.jurors.append(juror)

        # Non-participant community member
        self.community_user = User.objects.create_user(username='community_user', password='password123')
        UserProfile.objects.create(user=self.community_user, rewards=100)

    def test_non_participant_can_view_open_dispute(self):
        self.client.login(username='community_user', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dispute Details")
        self.assertContains(response, "Cast your jury vote")

    def test_task_participants_blocked_from_voting(self):
        # Taker attempts to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[self.dispute.id]),
            {'choice': 'taker_win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[self.dispute.id]),
            {'choice': 'poster_win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

    def test_duplicate_vote_prevention(self):
        self.client.login(username='juror1', password='password123')
        # First vote
        response = self.client.post(
            reverse('submit_vote', args=[self.dispute.id]),
            {'choice': 'taker_win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute).count(), 1)

        # Duplicate vote attempt
        response2 = self.client.post(
            reverse('submit_vote', args=[self.dispute.id]),
            {'choice': 'poster_win'}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute).count(), 1)
        vote = DisputeVote.objects.get(dispute=self.dispute, voter=self.jurors[0])
        self.assertEqual(vote.choice, 'taker_win')

    def test_jury_vote_submission_success(self):
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[self.dispute.id]),
            {'choice': 'taker_win'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.taker_votes_count, 1)
        self.assertEqual(self.dispute.total_votes_count, 1)

    def test_consensus_resolution_taker_win(self):
        # 4 jurors vote taker_win, 1 votes poster_win -> 80% supermajority (>= 66%)
        choices = ['taker_win', 'taker_win', 'taker_win', 'poster_win', 'taker_win']
        for idx, juror in enumerate(self.jurors):
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('submit_vote', args=[self.dispute.id]),
                {'choice': choices[idx]}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: original 500 + reward 200 + deposit refund 50 = 750
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 750)

    def test_consensus_resolution_poster_win(self):
        # 1 juror votes taker_win, 4 vote poster_win -> 80% supermajority for poster_win
        choices = ['poster_win', 'poster_win', 'taker_win', 'poster_win', 'poster_win']
        for idx, juror in enumerate(self.jurors):
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('submit_vote', args=[self.dispute.id]),
                {'choice': choices[idx]}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'forfeited')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster balance: 1000 + 200 (task reward refunded) + 50 (forfeited bond) = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

    def test_vote_without_quorum_does_not_settle(self):
        # Only 4 jurors vote taker_win (total votes = 4 < 5)
        for juror in self.jurors[:4]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('submit_vote', args=[self.dispute.id]),
                {'choice': 'taker_win'}
            )

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.dispute.escrow_status, 'held')
        self.assertEqual(self.task.status, 'disputed')


