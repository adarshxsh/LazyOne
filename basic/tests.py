from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Friendship, FriendRequest
from basic.services import select_and_lock_jurors, resolve_dispute, get_direct_friend_user_ids
from django.urls import reverse

class JurorSelectionAndStakeLockTests(TestCase):
    def setUp(self):
        # Create Task Poster and Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster)[0]
        self.poster_profile.rewards = 500
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.get_or_create(user=self.taker)[0]
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        # Friends of poster and taker
        self.friend_poster = User.objects.create_user(username='friend_poster', password='password123')
        self.friend_poster_profile = UserProfile.objects.get_or_create(user=self.friend_poster)[0]
        self.friend_poster_profile.rewards = 500
        self.friend_poster_profile.save()
        self.poster_profile.friends.add(self.friend_poster_profile)
        self.friend_poster_profile.friends.add(self.poster_profile)

        self.friend_taker = User.objects.create_user(username='friend_taker', password='password123')
        self.friend_taker_profile = UserProfile.objects.get_or_create(user=self.friend_taker)[0]
        self.friend_taker_profile.rewards = 500
        self.friend_taker_profile.save()
        self.taker_profile.friends.add(self.friend_taker_profile)
        self.friend_taker_profile.friends.add(self.taker_profile)

        # Neutral potential jurors with sufficient rewards (>= 100)
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.j1_profile = UserProfile.objects.get_or_create(user=self.juror1)[0]
        self.j1_profile.rewards = 500
        self.j1_profile.save()

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.j2_profile = UserProfile.objects.get_or_create(user=self.juror2)[0]
        self.j2_profile.rewards = 500
        self.j2_profile.save()

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.j3_profile = UserProfile.objects.get_or_create(user=self.juror3)[0]
        self.j3_profile.rewards = 500
        self.j3_profile.save()

        # Poor neutral user with < 100 rewards
        self.poor_user = User.objects.create_user(username='poor_user', password='password123')
        self.poor_profile = UserProfile.objects.get_or_create(user=self.poor_user)[0]
        self.poor_profile.rewards = 50
        self.poor_profile.save()

        # Task setup
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

        self.client = Client()

    def test_anti_bias_exclusion_and_stake_lock(self):
        """
        Tests AC 1, AC 2, AC 3:
        Jury selection excludes poster, taker, mutual/direct friends, and low balance users.
        Locks 100 points stake in ledger for assigned jurors.
        """
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not done'})
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = set(dispute.jurors.all())

        # Exclusions verification
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.friend_poster, assigned_jurors)
        self.assertNotIn(self.friend_taker, assigned_jurors)
        self.assertNotIn(self.poor_user, assigned_jurors)

        # Exact expected neutral panel
        self.assertEqual(assigned_jurors, {self.juror1, self.juror2, self.juror3})

        # Stake lock verification (500 - 100 = 400)
        for j in [self.juror1, self.juror2, self.juror3]:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 400)
            self.assertTrue(
                RewardLedger.objects.filter(
                    user=j, task=self.task, amount=-100, transaction_type='juror_stake_lock'
                ).exists()
            )

    def test_insufficient_neutral_juror_pool_fallback(self):
        """
        Tests fallback behavior when qualified pool is smaller than 3.
        """
        # Deactivate or reduce balance of juror3
        self.j3_profile.rewards = 10
        self.j3_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not done'}, follow=True)
        
        # Dispute should not be created
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

    def test_access_control_dispute_details_and_voting(self):
        """
        Tests AC 4:
        Dispute details and voting restricted exclusively to assigned jurors and counterparties.
        """
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute')
        select_and_lock_jurors(dispute, panel_size=3, stake_amount=100)

        unauthorized_user = User.objects.create_user(username='stranger', password='password123')

        # Unauthorized user viewing dispute detail
        self.client.login(username='stranger', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res.status_code, 302) # Redirects to home

        # Assigned juror viewing dispute detail
        self.client.login(username='juror1', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res.status_code, 200)

        # Counterparty viewing dispute detail
        self.client.login(username='poster', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res.status_code, 200)

        # Unauthorized user voting attempt
        self.client.login(username='stranger', password='password123')
        res = self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=unauthorized_user).exists())

        # Counterparty voting attempt (counterparties cannot vote)
        self.client.login(username='poster', password='password123')
        res = self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': self.taker.id})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.poster).exists())

    def test_stake_settlement_and_reward_redistribution(self):
        """
        Tests AC 5:
        Stake settlement correctly releases locked points to majority voters and distributes reward share from slashed minority voters.
        """
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute')
        select_and_lock_jurors(dispute, panel_size=3, stake_amount=100)

        # Juror 1 votes Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': self.taker.id})

        # Juror 2 votes Taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': self.taker.id})

        # Juror 3 votes Poster (minority)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'voted_for': self.poster.id})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Resolution checks
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'completed')

        # Taker received task reward (500 + 100 = 600)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 600)

        # Majority jurors (juror1, juror2) balance check:
        # Initial: 500, locked: -100 -> 400.
        # Returned stake: +100, reward share from 1 slashed juror (100 // 2 = 50): +50.
        # Total final = 400 + 100 + 50 = 550
        for j in [self.j1_profile, self.j2_profile]:
            j.refresh_from_db()
            self.assertEqual(j.rewards, 550)

        # Minority juror (juror3) balance check:
        # Initial: 500, locked: -100 -> 400. Slashed: +0 returned.
        # Total final = 400
        self.j3_profile.refresh_from_db()
        self.assertEqual(self.j3_profile.rewards, 400)

        # Check ledger entries for juror 1
        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_return', amount=100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward', amount=50).exists())

        # Check ledger entry for minority juror 3
        self.assertTrue(RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash', amount=0).exists())

    def test_active_dispute_juror_limit(self):
        """
        Tests constraint: A user cannot serve as a juror on more than 2 active disputes simultaneously.
        """
        # Create 2 open disputes and assign juror1 to both
        task1 = Task.objects.create(title='T1', description='D', reward=50, posted_by=self.poster, taken_by=self.taker, status='in_progress')
        dispute1 = Dispute.objects.create(task=task1, raised_by=self.taker, reason='R1')
        dispute1.jurors.add(self.juror1)

        task2 = Task.objects.create(title='T2', description='D', reward=50, posted_by=self.poster, taken_by=self.taker, status='in_progress')
        dispute2 = Dispute.objects.create(task=task2, raised_by=self.taker, reason='R2')
        dispute2.jurors.add(self.juror1)

        # juror1 is now on 2 active open disputes.
        # Try to select jurors for a 3rd dispute.
        # Need 3 available neutral jurors. Let's create juror4, juror5.
        juror4 = User.objects.create_user(username='juror4', password='password123')
        j4_p = UserProfile.objects.get_or_create(user=juror4)[0]
        j4_p.rewards = 500
        j4_p.save()

        juror5 = User.objects.create_user(username='juror5', password='password123')
        j5_p = UserProfile.objects.get_or_create(user=juror5)[0]
        j5_p.rewards = 500
        j5_p.save()

        dispute3 = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='R3')
        success = select_and_lock_jurors(dispute3, panel_size=3, stake_amount=100)
        self.assertTrue(success)

        dispute3_jurors = set(dispute3.jurors.all())
        # juror1 must be excluded because it's already on 2 active disputes
        self.assertNotIn(self.juror1, dispute3_jurors)
        self.assertEqual(dispute3_jurors, {self.juror2, self.juror3, juror4} | {juror5} & dispute3_jurors)

    def test_dispute_withdrawal_returns_stakes(self):
        """
        Tests that withdrawing a dispute returns locked stakes to jurors.
        """
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute')
        select_and_lock_jurors(dispute, panel_size=3, stake_amount=100)

        for j in [self.j1_profile, self.j2_profile, self.j3_profile]:
            j.refresh_from_db()
            self.assertEqual(j.rewards, 400)

        self.client.login(username='taker', password='password123')
        res = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertEqual(res.status_code, 302)

        for j in [self.j1_profile, self.j2_profile, self.j3_profile]:
            j.refresh_from_db()
            self.assertEqual(j.rewards, 500)
