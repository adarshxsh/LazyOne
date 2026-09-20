from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment, Friendship


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Candidate neutral jurors
        for i in range(1, 4):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)

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


class JurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='p_poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='p_taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.task = Task.objects.create(
            title="Dispute Task",
            description="Task for dispute testing",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    def test_dynamic_juror_selection_friend_and_counterparty_exclusion(self):
        # Poster friend via UserProfile.friends
        poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        poster_friend_profile = UserProfile.objects.create(user=poster_friend, rewards=500)
        self.poster_profile.friends.add(poster_friend_profile)

        # Taker friend via Friendship model
        taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        taker_friend_profile = UserProfile.objects.create(user=taker_friend, rewards=500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=taker_friend_profile)

        # Low rewards user (rewards < 100)
        low_reward_user = User.objects.create_user(username='low_reward', password='password123')
        UserProfile.objects.create(user=low_reward_user, rewards=50)

        # 3 Eligible neutral candidates
        candidate1 = User.objects.create_user(username='neutral_1', password='password123')
        UserProfile.objects.create(user=candidate1, rewards=150)

        candidate2 = User.objects.create_user(username='neutral_2', password='password123')
        UserProfile.objects.create(user=candidate2, rewards=150)

        candidate3 = User.objects.create_user(username='neutral_3', password='password123')
        UserProfile.objects.create(user=candidate3, rewards=150)

        self.client.login(username='p_taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        assignments = list(dispute.juror_assignments.all())
        self.assertEqual(len(assignments), 3)

        assigned_user_ids = [a.juror.id for a in assignments]
        # Verify counterparties and friends are excluded
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)
        self.assertNotIn(poster_friend.id, assigned_user_ids)
        self.assertNotIn(taker_friend.id, assigned_user_ids)
        self.assertNotIn(low_reward_user.id, assigned_user_ids)

        # Verify only eligible candidates were assigned
        self.assertIn(candidate1.id, assigned_user_ids)
        self.assertIn(candidate2.id, assigned_user_ids)
        self.assertIn(candidate3.id, assigned_user_ids)

        # Verify stake bond deduction (150 - 20 = 130) and RewardLedger entry
        for c in [candidate1, candidate2, candidate3]:
            c.userprofile.refresh_from_db()
            self.assertEqual(c.userprofile.rewards, 130)
            ledger = RewardLedger.objects.filter(user=c, transaction_type='juror_stake').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -20)

    def test_dynamic_juror_selection_insufficient_candidates_admin_review(self):
        # Only 2 candidates available
        c1 = User.objects.create_user(username='cand_1', password='password123')
        UserProfile.objects.create(user=c1, rewards=200)
        c2 = User.objects.create_user(username='cand_2', password='password123')
        UserProfile.objects.create(user=c2, rewards=200)

        self.client.login(username='p_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'admin_review')
        self.assertTrue(dispute.requires_admin_review)
        self.assertEqual(dispute.juror_assignments.count(), 0)

    def test_dispute_detail_access_control(self):
        c1 = User.objects.create_user(username='j1', password='password123')
        UserProfile.objects.create(user=c1, rewards=200)
        c2 = User.objects.create_user(username='j2', password='password123')
        UserProfile.objects.create(user=c2, rewards=200)
        c3 = User.objects.create_user(username='j3', password='password123')
        UserProfile.objects.create(user=c3, rewards=200)

        self.client.login(username='p_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Unassigned user
        outsider = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=outsider, rewards=200)

        self.client.login(username='outsider', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Assigned juror
        self.client.login(username='j1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_juror_voting_and_dispute_resolution(self):
        c1 = User.objects.create_user(username='voter_1', password='password123')
        UserProfile.objects.create(user=c1, rewards=200)
        c2 = User.objects.create_user(username='voter_2', password='password123')
        UserProfile.objects.create(user=c2, rewards=200)
        c3 = User.objects.create_user(username='voter_3', password='password123')
        UserProfile.objects.create(user=c3, rewards=200)

        self.client.login(username='p_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Jurors 1 & 2 vote for Taker, Juror 3 votes for Poster
        self.client.login(username='voter_1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='voter_2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.taker.id})

        self.client.login(username='voter_3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Majority voters (1 & 2) get 200 - 20 + 30 = 210
        c1.userprofile.refresh_from_db()
        self.assertEqual(c1.userprofile.rewards, 210)

        # Minority voter (3) gets no payout (200 - 20 = 180)
        c3.userprofile.refresh_from_db()
        self.assertEqual(c3.userprofile.rewards, 180)

    def test_blocked_direct_chat_between_co_jurors(self):
        c1 = User.objects.create_user(username='co_j1', password='password123')
        UserProfile.objects.create(user=c1, rewards=200)
        c2 = User.objects.create_user(username='co_j2', password='password123')
        UserProfile.objects.create(user=c2, rewards=200)
        c3 = User.objects.create_user(username='co_j3', password='password123')
        UserProfile.objects.create(user=c3, rewards=200)

        self.client.login(username='p_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        # co_j1 tries to start direct chat with co_j2
        self.client.login(username='co_j1', password='password123')
        response = self.client.get(reverse('start_chat', args=[c2.id]))
        self.assertRedirects(response, reverse('home'))


