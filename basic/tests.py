from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorCommitment, JurorVote


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

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=1500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=1500)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Service Task",
            description="Task description",
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Service specification disagreement",
            deposit_amount=100,
            status='open',
            phase='commit'
        )

    def test_juror_commitment_model_and_hash_computation(self):
        choice = 'poster'
        salt = 'super_secret_salt_12345'
        expected_hash = JurorCommitment.compute_hash(choice, salt, self.juror1.id)

        commitment = JurorCommitment.objects.create(
            dispute=self.dispute,
            juror=self.juror1,
            commitment_hash=expected_hash
        )

        self.assertEqual(commitment.commitment_hash, expected_hash)
        self.assertTrue(commitment.verify_commitment(choice, salt))
        self.assertFalse(commitment.verify_commitment('taker', salt))

    def test_commit_phase_endpoint_salt_entropy_and_privacy(self):
        self.client.login(username='juror1', password='password123')

        # Attempt commit with short salt (< 16 chars)
        response = self.client.post(
            reverse('submit_commitment', args=[self.dispute.id]),
            {'choice': 'poster', 'salt': 'short_salt'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(JurorCommitment.objects.filter(dispute=self.dispute, juror=self.juror1).exists())

        # Valid commit with >= 16 char salt
        valid_salt = 'valid_long_secret_salt_98765'
        response = self.client.post(
            reverse('submit_commitment', args=[self.dispute.id]),
            {'choice': 'poster', 'salt': valid_salt}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        commitment = JurorCommitment.objects.get(dispute=self.dispute, juror=self.juror1)
        expected_hash = JurorCommitment.compute_hash('poster', valid_salt, self.juror1.id)
        self.assertEqual(commitment.commitment_hash, expected_hash)

        # Check detail view during commit phase does not expose vote tallies
        detail_response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertNotIn('poster_votes', detail_response.context)
        self.assertNotIn('taker_votes', detail_response.context)

    def test_reveal_vote_endpoint_verification_and_consensus(self):
        salt1 = 'juror1_secret_salt_12345678'
        salt2 = 'juror2_secret_salt_87654321'

        # Juror 1 commits 'poster'
        hash1 = JurorCommitment.compute_hash('poster', salt1, self.juror1.id)
        JurorCommitment.objects.create(dispute=self.dispute, juror=self.juror1, commitment_hash=hash1)

        # Juror 2 commits 'poster'
        hash2 = JurorCommitment.compute_hash('poster', salt2, self.juror2.id)
        JurorCommitment.objects.create(dispute=self.dispute, juror=self.juror2, commitment_hash=hash2)

        # Transition to reveal phase
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('transition_phase', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.phase, 'reveal')

        # Juror 1 attempts reveal with wrong salt
        self.client.login(username='juror1', password='password123')
        res_fail = self.client.post(
            reverse('reveal_vote', args=[self.dispute.id]),
            {'choice': 'poster', 'salt': 'wrong_salt_1234567890'}
        )
        self.assertRedirects(res_fail, reverse('dispute_detail', args=[self.dispute.id]))
        vote_fail = JurorVote.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertFalse(vote_fail.is_verified)

        # Check penalization ledger
        slash = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_slash').first()
        self.assertIsNotNone(slash)

        # Reset vote for valid reveal test
        vote_fail.delete()

        # Juror 1 reveals correctly
        res_ok1 = self.client.post(
            reverse('reveal_vote', args=[self.dispute.id]),
            {'choice': 'poster', 'salt': salt1}
        )
        self.assertRedirects(res_ok1, reverse('dispute_detail', args=[self.dispute.id]))
        v1 = JurorVote.objects.get(dispute=self.dispute, juror=self.juror1)
        self.assertTrue(v1.is_verified)

        # Juror 2 reveals correctly -> triggers consensus calculation
        self.client.login(username='juror2', password='password123')
        res_ok2 = self.client.post(
            reverse('reveal_vote', args=[self.dispute.id]),
            {'choice': 'poster', 'salt': salt2}
        )
        self.assertRedirects(res_ok2, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.phase, 'concluded')
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winning_choice, 'poster')

        # Detail view now displays concluded stats
        final_detail = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(final_detail.context['poster_votes'], 2)
        self.assertEqual(final_detail.context['total_votes'], 2)


