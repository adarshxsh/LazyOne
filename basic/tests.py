from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import hashlib
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeCommitment


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


class CommitRevealDisputeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        UserProfile.objects.create(user=self.juror1, rewards=1000)
        UserProfile.objects.create(user=self.juror2, rewards=1000)
        UserProfile.objects.create(user=self.juror3, rewards=1000)

        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task with dispute",
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work quality issue",
            deposit_amount=100,
            escrow_status='held',
            voting_phase='commit',
            commit_deadline=timezone.now() + timedelta(hours=1),
            reveal_deadline=timezone.now() + timedelta(hours=2)
        )
        self.dispute.jurors.add(self.juror1, self.juror2, self.juror3)

    def test_commit_phase_accepts_hash_without_revealing_choice(self):
        self.client.login(username='juror1', password='password123')
        salt = 'secret_salt_123'
        choice = 'poster'
        commitment_hash = DisputeCommitment.compute_hash(choice, salt, self.juror1.id)

        response = self.client.post(
            reverse('commit_dispute_vote', args=[self.dispute.id]),
            {'commitment_hash': commitment_hash}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        commitment = DisputeCommitment.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertEqual(commitment.commitment_hash, commitment_hash)
        self.assertIsNone(commitment.vote_choice)
        self.assertFalse(commitment.revealed)

        # Verify privacy: status API hides vote counts and choices during commit phase
        status_resp = self.client.get(reverse('dispute_status_api', args=[self.dispute.id]))
        data = status_resp.json()
        self.assertEqual(data['phase'], 'commit')
        self.assertIsNone(data['tallies'])
        self.assertIsNone(data['revealed_votes'])

    def test_reveal_phase_verifies_hash_and_tallies_upon_finish(self):
        salt = 'secret_salt_456'
        choice = 'taker'
        commitment_hash = DisputeCommitment.compute_hash(choice, salt, self.juror1.id)

        # Commit phase
        DisputeCommitment.objects.create(
            dispute=self.dispute,
            juror=self.juror1,
            commitment_hash=commitment_hash
        )

        # Transition dispute to reveal phase
        self.dispute.voting_phase = 'reveal'
        self.dispute.save()

        self.client.login(username='juror1', password='password123')

        # Invalid reveal attempt (wrong salt)
        bad_resp = self.client.post(
            reverse('reveal_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': choice, 'salt': 'wrong_salt'}
        )
        commitment = DisputeCommitment.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertFalse(commitment.revealed)

        # Valid reveal attempt
        good_resp = self.client.post(
            reverse('reveal_dispute_vote', args=[self.dispute.id]),
            {'vote_choice': choice, 'salt': salt}
        )
        self.assertRedirects(good_resp, reverse('dispute_detail', args=[self.dispute.id]))
        commitment.refresh_from_db()
        self.assertTrue(commitment.revealed)
        self.assertEqual(commitment.vote_choice, 'taker')

    def test_unrevealed_commitments_expire_at_deadline(self):
        # Juror 1 commits and reveals 'poster'
        salt1 = 'salt1'
        hash1 = DisputeCommitment.compute_hash('poster', salt1, self.juror1.id)
        c1 = DisputeCommitment.objects.create(dispute=self.dispute, juror=self.juror1, commitment_hash=hash1)

        # Juror 2 commits but DOES NOT reveal
        salt2 = 'salt2'
        hash2 = DisputeCommitment.compute_hash('taker', salt2, self.juror2.id)
        c2 = DisputeCommitment.objects.create(dispute=self.dispute, juror=self.juror2, commitment_hash=hash2)

        # Move to reveal phase and reveal juror 1 only
        self.dispute.voting_phase = 'reveal'
        self.dispute.save()

        c1.vote_choice = 'poster'
        c1.salt = salt1
        c1.revealed = True
        c1.save()

        # Set reveal deadline in the past
        self.dispute.reveal_deadline = timezone.now() - timedelta(minutes=1)
        self.dispute.save()

        # Check phase auto-advances to finished and drops unrevealed votes
        phase = self.dispute.get_current_phase()
        self.assertEqual(phase, 'finished')

        tallies = self.dispute.tally_votes()
        self.assertEqual(tallies['poster'], 1)
        self.assertEqual(tallies['taker'], 0)
        self.assertEqual(tallies['total'], 1)
        self.assertEqual(self.dispute.status, 'resolved')

