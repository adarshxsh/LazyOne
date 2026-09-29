from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
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


class DisputeVotingAndQuorumTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=100)

        self.task = Task.objects.create(
            title="Disputed Jury Task",
            description="Testing decentralized jury voting",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair rejection of work",
            deposit_amount=50,
            escrow_status='held',
            quorum_threshold=3
        )

    def test_peer_user_access_to_open_dispute(self):
        # Peer user (juror1) logs in and views open dispute detail
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disputed Jury Task")
        self.assertTrue(response.context['can_vote'])

    def test_resolved_dispute_access_restriction(self):
        # Resolve dispute
        self.dispute.status = 'resolved'
        self.dispute.save()

        # Non-participant peer user gets redirected
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Participant (poster) can still view resolved dispute
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_participants_cannot_vote(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_single_vote_constraint_per_user(self):
        self.client.login(username='juror1', password='password123')
        # First vote succeeds
        response1 = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.taker.id}
        )
        self.assertRedirects(response1, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

        # Second vote is rejected
        response2 = self.client.post(
            reverse('cast_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)

    def test_reaching_quorum_threshold_resolves_dispute_and_releases_funds(self):
        # Juror 1 votes for taker (1/3)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # Juror 2 votes for taker (2/3)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': self.taker.id})
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # Juror 3 votes for poster (3/3 -> quorum reached, taker wins 2-1)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'voted_for': self.poster.id})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker received reward (200) + deposit refund (50) -> 200 + 200 + 50 = 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

    def test_resolve_expired_disputes_command_uses_vote_majority(self):
        # Dispute created 10 days ago (expired)
        self.dispute.created_at = timezone.now() - timedelta(days=10)
        self.dispute.save()

        # Cast 2 votes: 1 for poster, 0 for taker (poster has majority)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, voted_for=self.poster)

        call_command('resolve_expired_disputes', days=7)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster received task reward refund (200) + forfeited deposit bond (50) -> 1000 + 200 + 50 = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)


