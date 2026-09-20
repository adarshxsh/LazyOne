from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import (
    UserProfile, Task, Dispute, RewardLedger, Conversation,
    JuryAssignment, DisputeVote, Friendship, FriendRequest
)


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


class JurorSelectionAndStakeLockTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster_juror_test', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000, is_phone_verified=True)

        self.taker = User.objects.create_user(username='taker_juror_test', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000, is_phone_verified=True)

        # Create task
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Test Description",
            reward=300, # 10% is 30, min stake 50 applies
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def _create_qualified_candidate(self, username, rewards=500):
        user = User.objects.create_user(username=username, password='password123')
        profile = UserProfile.objects.create(user=user, rewards=rewards, is_phone_verified=True)
        # Create 3 completed tasks
        for i in range(3):
            Task.objects.create(
                title=f"Task {i} for {username}",
                description="Done",
                reward=100,
                posted_by=self.poster,
                taken_by=user,
                status='completed'
            )
        return user, profile

    def test_juror_stake_calculation(self):
        dispute = Dispute(task=self.task, raised_by=self.taker, reason="test")
        self.assertEqual(dispute.calculate_juror_stake(), 50)

        # High reward task
        high_task = Task.objects.create(
            title="High Task", description="desc", reward=1200,
            posted_by=self.poster, taken_by=self.taker, status='in_progress'
        )
        high_dispute = Dispute(task=high_task, raised_by=self.taker, reason="test")
        # 10% of 1200 = 120 (> 50)
        self.assertEqual(high_dispute.calculate_juror_stake(), 120)

    def test_raise_dispute_selects_neutral_jurors_and_locks_stake(self):
        # Create 3 qualified candidate jurors
        juror1, profile1 = self._create_qualified_candidate("juror_c1")
        juror2, profile2 = self._create_qualified_candidate("juror_c2")
        juror3, profile3 = self._create_qualified_candidate("juror_c3")

        self.client.login(username='taker_juror_test', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unfulfilled'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Check jury assignments
        assignments = list(JuryAssignment.objects.filter(dispute=dispute))
        self.assertEqual(len(assignments), 3)

        assigned_user_ids = {a.juror.id for a in assignments}
        self.assertEqual(assigned_user_ids, {juror1.id, juror2.id, juror3.id})

        # Check locked stakes (50 points deducted from each)
        for p in [profile1, profile2, profile3]:
            p.refresh_from_db()
            self.assertEqual(p.rewards, 450)

        # Check RewardLedger entries
        for j in [juror1, juror2, juror3]:
            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -50)

    def test_anti_bias_filtering_excludes_poster_taker_and_direct_friends(self):
        # Candidate 1: Direct friend of poster via UserProfile.friends
        friend_poster, fp_profile = self._create_qualified_candidate("friend_poster")
        self.poster_profile.friends.add(fp_profile)

        # Candidate 2: Direct friend of taker via Friendship model
        friend_taker, ft_profile = self._create_qualified_candidate("friend_taker")
        Friendship.objects.create(from_user=self.taker_profile, to_user=ft_profile)

        # Candidate 3: Direct friend of poster via accepted FriendRequest
        friend_req_user, fr_profile = self._create_qualified_candidate("friend_req_user")
        FriendRequest.objects.create(from_user=self.poster, to_user=friend_req_user, is_accepted=True)

        # Neutral qualified candidates 4, 5, 6
        neutral1, n1_p = self._create_qualified_candidate("neutral1")
        neutral2, n2_p = self._create_qualified_candidate("neutral2")
        neutral3, n3_p = self._create_qualified_candidate("neutral3")

        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Bias check'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_user_ids = set(JuryAssignment.objects.filter(dispute=dispute).values_list('juror_id', flat=True))

        # Must NOT include poster, taker, or any direct friends
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)
        self.assertNotIn(friend_poster.id, assigned_user_ids)
        self.assertNotIn(friend_taker.id, assigned_user_ids)
        self.assertNotIn(friend_req_user.id, assigned_user_ids)

        # MUST include neutral candidates
        self.assertEqual(assigned_user_ids, {neutral1.id, neutral2.id, neutral3.id})

    def test_filtering_excludes_unverified_insufficient_rewards_fewer_tasks(self):
        # Unverified user (0 completed tasks, unverified)
        u_unverified = User.objects.create_user(username="unverified_user", password="password123")
        UserProfile.objects.create(user=u_unverified, rewards=500, is_phone_verified=False, is_instagram_verified=False)

        # User with fewer than 3 completed tasks
        u_few_tasks = User.objects.create_user(username="few_tasks_user", password="password123")
        UserProfile.objects.create(user=u_few_tasks, rewards=500, is_phone_verified=True)
        Task.objects.create(title="T1", description="d", reward=50, posted_by=self.poster, taken_by=u_few_tasks, status='completed')

        # User with insufficient reward balance (< 50)
        u_low_balance = User.objects.create_user(username="low_bal_user", password="password123")
        UserProfile.objects.create(user=u_low_balance, rewards=20, is_phone_verified=True)
        for i in range(3):
            Task.objects.create(title=f"T{i}", description="d", reward=50, posted_by=self.poster, taken_by=u_low_balance, status='completed')

        # 3 Qualified neutral users
        n1, _ = self._create_qualified_candidate("q_neutral1")
        n2, _ = self._create_qualified_candidate("q_neutral2")
        n3, _ = self._create_qualified_candidate("q_neutral3")

        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Qualification check'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_user_ids = set(JuryAssignment.objects.filter(dispute=dispute).values_list('juror_id', flat=True))

        self.assertNotIn(u_unverified.id, assigned_user_ids)
        self.assertNotIn(u_few_tasks.id, assigned_user_ids)
        self.assertNotIn(u_low_balance.id, assigned_user_ids)
        self.assertEqual(assigned_user_ids, {n1.id, n2.id, n3.id})

    def test_withdraw_dispute_releases_juror_stakes(self):
        juror1, profile1 = self._create_qualified_candidate("w_juror1")
        juror2, profile2 = self._create_qualified_candidate("w_juror2")

        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Will withdraw'}
        )

        dispute = Dispute.objects.get(task=self.task)
        profile1.refresh_from_db()
        self.assertEqual(profile1.rewards, 450)

        # Withdraw dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        # Stakes restored (450 -> 500)
        profile1.refresh_from_db()
        profile2.refresh_from_db()
        self.assertEqual(profile1.rewards, 500)
        self.assertEqual(profile2.rewards, 500)

        for j in [juror1, juror2]:
            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_release').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, 50)

    def test_resolving_dispute_releases_stakes_and_awards_voting_incentives(self):
        juror1, profile1 = self._create_qualified_candidate("r_juror1")
        juror2, profile2 = self._create_qualified_candidate("r_juror2")

        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Resolution test'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Record vote for juror1
        DisputeVote.objects.create(dispute=dispute, voter=juror1, choice='taker')

        # Resolve dispute via resolve_jury_stakes
        dispute.resolve_jury_stakes(incentive_per_voter=20)

        # Juror 1 voted: 450 + 50 (stake release) + 20 (reward) = 520
        profile1.refresh_from_db()
        self.assertEqual(profile1.rewards, 520)

        # Juror 2 did not vote: 450 + 50 (stake release) = 500
        profile2.refresh_from_db()
        self.assertEqual(profile2.rewards, 500)

        # Check ledger entries
        j1_release = RewardLedger.objects.filter(user=juror1, transaction_type='juror_release').first()
        self.assertIsNotNone(j1_release)
        self.assertEqual(j1_release.amount, 50)

        j1_reward = RewardLedger.objects.filter(user=juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(j1_reward)
        self.assertEqual(j1_reward.amount, 20)


