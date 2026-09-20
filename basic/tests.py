from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryPool, JurorAssignment, DisputeVote
from .views.dispute import check_and_replace_inactive_jurors


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


class StakedJuryPoolConsensusTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Task Poster and Taker
        self.poster = User.objects.create_user(username='poster_juror_test', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_juror_test', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        # Create Task
        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Jury Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

        # Create low balance user (rewards < 20)
        self.low_balance_user = User.objects.create_user(username='poor_user', password='password123')
        UserProfile.objects.create(user=self.low_balance_user, rewards=10)

        # Create 5 neutral eligible users with sufficient rewards
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=100)
            self.jurors.append(juror)

        # Create 1 extra neutral user for replacement test
        self.backup_juror = User.objects.create_user(username='backup_juror', password='password123')
        UserProfile.objects.create(user=self.backup_juror, rewards=100)

    def test_jury_pool_auto_creation_and_party_exclusion(self):
        # Taker raises dispute
        self.client.login(username='taker_juror_test', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury_pool'))
        jury_pool = dispute.jury_pool

        self.assertEqual(jury_pool.pool_size, 5)
        self.assertEqual(jury_pool.required_stake, 20)
        self.assertEqual(jury_pool.status, 'active')

        assigned_user_ids = set(
            JurorAssignment.objects.filter(jury_pool=jury_pool, is_active=True).values_list('user_id', flat=True)
        )

        self.assertEqual(len(assigned_user_ids), 5)
        # Exclusion checks: poster, taker, and low_balance_user must NOT be assigned
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)
        self.assertNotIn(self.low_balance_user.id, assigned_user_ids)

    def test_juror_voting_stake_deduction_and_ledger(self):
        # Raise dispute
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Pick one assigned juror
        assignment = JurorAssignment.objects.filter(jury_pool=dispute.jury_pool, is_active=True).first()
        juror = assignment.user

        # Juror votes for taker
        self.client.login(username=juror.username, password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[dispute.id]),
            {'vote': 'taker'}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Check balance deducted: 100 - 20 = 80
        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, 80)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_stake').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -20)

        # Check DisputeVote and Assignment status
        vote = DisputeVote.objects.get(jury_pool=dispute.jury_pool, juror=juror)
        self.assertEqual(vote.vote_for, self.taker)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'voted')

    def test_unauthorized_voting_blocked(self):
        # Raise dispute
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster attempts to vote
        self.client.login(username='poster_juror_test', password='password123')
        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertFalse(DisputeVote.objects.filter(jury_pool=dispute.jury_pool, juror=self.poster).exists())

        # Low balance user attempts to vote
        self.client.login(username='poor_user', password='password123')
        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertFalse(DisputeVote.objects.filter(jury_pool=dispute.jury_pool, juror=self.low_balance_user).exists())

    def test_consensus_threshold_poster_wins(self):
        # Taker raises dispute: reward=200, 20% = 50 deposit bond deducted from taker (200 - 50 = 150)
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        assignments = list(JurorAssignment.objects.filter(jury_pool=dispute.jury_pool, is_active=True))
        # 3 jurors vote for poster (reaching threshold 3/5)
        for i in range(3):
            juror = assignments[i].user
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster'})

        dispute.refresh_from_db()
        dispute.task.refresh_from_db()

        # Dispute and JuryPool resolved
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.jury_pool.status, 'resolved')
        self.assertEqual(dispute.task.status, 'cancelled')

        # Poster gets task reward refunded (1000 + 200 = 1200)
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1200)

        # Taker deposit bond forfeited (50 points lost)
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # The 3 winning jurors get stake refunded (20) + share of 50 deposit bond (50 // 3 = 16)
        # Expected balance for each winning juror: 100 - 20 (staked) + 20 (refund) + 16 (reward) = 116
        for i in range(3):
            juror = assignments[i].user
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 116)

            # Check ledger entries
            refund_ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_refund').first()
            reward_ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_reward').first()
            self.assertIsNotNone(refund_ledger)
            self.assertIsNotNone(reward_ledger)
            self.assertEqual(reward_ledger.amount, 16)

    def test_consensus_threshold_taker_wins(self):
        # Taker raises dispute: deposit 50 deducted from taker (200 - 50 = 150)
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        assignments = list(JurorAssignment.objects.filter(jury_pool=dispute.jury_pool, is_active=True))

        # 3 jurors vote for taker
        for i in range(3):
            juror = assignments[i].user
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'taker'})

        dispute.refresh_from_db()
        dispute.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.task.status, 'completed')

        # Taker wins: gets task reward (200) + deposit bond refund (50)
        # Taker balance: 150 + 200 + 50 = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Winning jurors get 20 points stake refunded
        for i in range(3):
            juror = assignments[i].user
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 100)

    def test_inactive_juror_replacement(self):
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        assignment = JurorAssignment.objects.filter(jury_pool=dispute.jury_pool, is_active=True).first()
        inactive_user = assignment.user

        # Age the assignment beyond 48 hours
        assignment.assigned_at = timezone.now() - timedelta(hours=49)
        assignment.save()

        # Trigger check/replacement
        check_and_replace_inactive_jurors(dispute)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'replaced')
        self.assertFalse(assignment.is_active)

        # Check backup juror was assigned as active replacement
        new_assignment = JurorAssignment.objects.filter(jury_pool=dispute.jury_pool, user=self.backup_juror, is_active=True).first()
        self.assertIsNotNone(new_assignment)
        self.assertEqual(new_assignment.status, 'assigned')


