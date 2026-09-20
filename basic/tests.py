import hashlib
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryAssignment, DisputeVote


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


class CommitRevealVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        # Task & Dispute
        self.task = Task.objects.create(
            title="Commit Reveal Dispute Task",
            description="Testing two phase voting",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair review",
            deposit_amount=50,
            escrow_status='held',
            status='voting'
        )

        # Assign jurors
        JuryAssignment.objects.create(dispute=self.dispute, juror=self.juror1)
        JuryAssignment.objects.create(dispute=self.dispute, juror=self.juror2)
        JuryAssignment.objects.create(dispute=self.dispute, juror=self.juror3)

        # Start voting phase: 15 min commit, 15 min reveal
        self.dispute.start_voting_phase(commit_minutes=15, reveal_minutes=15)

    def test_submit_vote_commitment_success(self):
        self.client.login(username='juror1', password='password123')

        salt = "secret_salt_123"
        vote_choice = "taker"
        expected_hash = hashlib.sha256(f"{vote_choice}{salt}".encode('utf-8')).hexdigest()

        response = self.client.post(
            reverse('submit_vote_commitment', args=[self.dispute.id]),
            {'vote': vote_choice, 'salt': salt}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        vote = DisputeVote.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertEqual(vote.commitment_hash, expected_hash)
        self.assertEqual(vote.status, 'committed')
        self.assertIsNone(vote.choice)  # Choice must NOT be stored in commit phase

        assignment = JuryAssignment.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertEqual(assignment.status, 'committed')

    def test_commit_phase_hides_tallies_and_choices(self):
        # Juror1 commits vote
        salt1 = "secretsalt1"
        hash1 = hashlib.sha256(f"taker{salt1}".encode('utf-8')).hexdigest()
        DisputeVote.objects.create(
            dispute=self.dispute, juror=self.juror1, commitment_hash=hash1, status='committed'
        )

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['poster_votes'])
        self.assertIsNone(response.context['taker_votes'])
        self.assertIsNone(response.context['total_votes'])
        self.assertTrue(response.context['is_commit_phase'])

    def test_submit_vote_reveal_success(self):
        # First commit
        salt1 = "secretsalt1"
        vote_choice = "taker"
        commitment_hash = hashlib.sha256(f"{vote_choice}{salt1}".encode('utf-8')).hexdigest()

        vote = DisputeVote.objects.create(
            dispute=self.dispute, juror=self.juror1, commitment_hash=commitment_hash, status='committed'
        )

        # Fast forward to reveal phase
        self.dispute.voting_phase = 'reveal'
        self.dispute.commit_deadline = timezone.now() - timedelta(minutes=5)
        self.dispute.reveal_deadline = timezone.now() + timedelta(minutes=15)
        self.dispute.save()

        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('submit_vote_reveal', args=[self.dispute.id]),
            {'choice': vote_choice, 'salt': salt1}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        vote.refresh_from_db()
        self.assertEqual(vote.status, 'revealed')
        self.assertEqual(vote.choice, vote_choice)
        self.assertEqual(vote.salt, salt1)

    def test_reveal_rejects_short_salt_and_mismatch(self):
        # Commitment with salt 'secretsalt1' and vote 'taker'
        salt_valid = "secretsalt1"
        commitment_hash = hashlib.sha256(f"taker{salt_valid}".encode('utf-8')).hexdigest()

        DisputeVote.objects.create(
            dispute=self.dispute, juror=self.juror1, commitment_hash=commitment_hash, status='committed'
        )

        # Transition to reveal phase
        self.dispute.voting_phase = 'reveal'
        self.dispute.commit_deadline = timezone.now() - timedelta(minutes=5)
        self.dispute.reveal_deadline = timezone.now() + timedelta(minutes=15)
        self.dispute.save()

        self.client.login(username='juror1', password='password123')

        # Test short salt < 8 chars
        response_short = self.client.post(
            reverse('submit_vote_reveal', args=[self.dispute.id]),
            {'choice': 'taker', 'salt': 'short'}
        )
        self.assertRedirects(response_short, reverse('dispute_detail', args=[self.dispute.id]))
        vote = DisputeVote.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertEqual(vote.status, 'committed')  # Vote remains unrevealed

        # Test mismatched salt
        response_mismatch = self.client.post(
            reverse('submit_vote_reveal', args=[self.dispute.id]),
            {'choice': 'taker', 'salt': 'wrongsecretsalt'}
        )
        self.assertRedirects(response_mismatch, reverse('dispute_detail', args=[self.dispute.id]))
        vote.refresh_from_db()
        self.assertEqual(vote.status, 'committed')  # Vote remains unrevealed

    def test_unrevealed_commitments_expire_and_settle(self):
        # Juror 1 commits & reveals 'taker'
        salt1 = "secretsalt1"
        hash1 = hashlib.sha256(f"taker{salt1}".encode('utf-8')).hexdigest()
        DisputeVote.objects.create(
            dispute=self.dispute, juror=self.juror1, commitment_hash=hash1, status='committed'
        )

        # Juror 2 commits but never reveals
        salt2 = "secretsalt2"
        hash2 = hashlib.sha256(f"poster{salt2}".encode('utf-8')).hexdigest()
        DisputeVote.objects.create(
            dispute=self.dispute, juror=self.juror2, commitment_hash=hash2, status='committed'
        )

        # Transition to reveal phase and reveal Juror 1
        self.dispute.voting_phase = 'reveal'
        self.dispute.commit_deadline = timezone.now() - timedelta(minutes=20)
        self.dispute.reveal_deadline = timezone.now() + timedelta(minutes=10)
        self.dispute.save()

        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('submit_vote_reveal', args=[self.dispute.id]),
            {'choice': 'taker', 'salt': salt1}
        )

        # Now expire reveal deadline
        self.dispute.reveal_deadline = timezone.now() - timedelta(minutes=5)
        self.dispute.save()

        # Check phase update & expiration
        self.dispute.check_and_update_phase()

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.voting_phase, 'closed')

        # Juror 2 unrevealed vote is marked expired
        vote2 = DisputeVote.objects.get(dispute=self.dispute, juror=self.juror2)
        self.assertEqual(vote2.status, 'expired')

        # Task is completed (since Juror 1's valid vote for taker won)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

    def test_all_jurors_reveal_triggers_final_settlement(self):
        # All 3 jurors commit
        salt1, salt2, salt3 = "secretsalt1", "secretsalt2", "secretsalt3"
        hash1 = hashlib.sha256(f"taker{salt1}".encode('utf-8')).hexdigest()
        hash2 = hashlib.sha256(f"taker{salt2}".encode('utf-8')).hexdigest()
        hash3 = hashlib.sha256(f"poster{salt3}".encode('utf-8')).hexdigest()

        DisputeVote.objects.create(dispute=self.dispute, juror=self.juror1, commitment_hash=hash1, status='committed')
        DisputeVote.objects.create(dispute=self.dispute, juror=self.juror2, commitment_hash=hash2, status='committed')
        DisputeVote.objects.create(dispute=self.dispute, juror=self.juror3, commitment_hash=hash3, status='committed')

        # Transition to reveal phase
        self.dispute.voting_phase = 'reveal'
        self.dispute.commit_deadline = timezone.now() - timedelta(minutes=5)
        self.dispute.reveal_deadline = timezone.now() + timedelta(minutes=15)
        self.dispute.save()

        # Reveal Juror 1
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('submit_vote_reveal', args=[self.dispute.id]), {'choice': 'taker', 'salt': salt1})

        # Reveal Juror 2
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('submit_vote_reveal', args=[self.dispute.id]), {'choice': 'taker', 'salt': salt2})

        # Reveal Juror 3 -> completes reveal phase
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('submit_vote_reveal', args=[self.dispute.id]), {'choice': 'poster', 'salt': salt3})

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.voting_phase, 'closed')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check juror rewards allocated
        ledger1 = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(ledger1)
        self.assertGreater(ledger1.amount, 0)


