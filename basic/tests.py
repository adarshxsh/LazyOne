from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeJuror, RewardLedger, Conversation, Friendship, FriendRequest


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Candidate neutral verified jurors for panel assignment
        for i in range(1, 4):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000, is_phone_verified=True)

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


class AutomatedDisputeJurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000, is_phone_verified=True)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500, is_phone_verified=True)

        self.task = Task.objects.create(
            title="Dispute Task",
            description="Testing dispute jury selection",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    def test_anti_bias_and_friends_exclusion(self):
        # Create user linked via M2M friends to poster
        friend_poster = User.objects.create_user(username='friend_poster', password='password123')
        fp_profile = UserProfile.objects.create(user=friend_poster, rewards=1000, is_phone_verified=True)
        self.poster_profile.friends.add(fp_profile)

        # Create user linked via Friendship to taker
        friend_taker = User.objects.create_user(username='friend_taker', password='password123')
        ft_profile = UserProfile.objects.create(user=friend_taker, rewards=1000, is_phone_verified=True)
        Friendship.objects.create(from_user=self.taker_profile, to_user=ft_profile)

        # Create user linked via FriendRequest to poster
        friend_req = User.objects.create_user(username='friend_req', password='password123')
        UserProfile.objects.create(user=friend_req, rewards=1000, is_phone_verified=True)
        FriendRequest.objects.create(from_user=self.poster, to_user=friend_req)

        # Create 3 neutral verified candidates
        neutrals = []
        for i in range(1, 4):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000, is_phone_verified=True)
            neutrals.append(u)

        self.client.login(username='taker_user', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality issue'})
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        assigned_users = set(dispute.jurors.values_list('user_id', flat=True))
        self.assertEqual(len(assigned_users), 3)

        # Excluded users MUST NOT be in assigned_users
        self.assertNotIn(self.poster.id, assigned_users)
        self.assertNotIn(self.taker.id, assigned_users)
        self.assertNotIn(friend_poster.id, assigned_users)
        self.assertNotIn(friend_taker.id, assigned_users)
        self.assertNotIn(friend_req.id, assigned_users)

        # All 3 assigned users must be from neutrals
        for neutral in neutrals:
            self.assertIn(neutral.id, assigned_users)

    def test_unverified_and_low_balance_exclusion(self):
        # 3 unverified users
        for i in range(1, 4):
            u = User.objects.create_user(username=f'unverified_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000, is_phone_verified=False, is_instagram_verified=False)

        # 3 low balance users (rewards = 100, which is < 250 for 50-point stake lock <= 20% balance)
        for i in range(1, 4):
            u = User.objects.create_user(username=f'low_balance_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100, is_phone_verified=True)

        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Escalation required'})

        dispute = Dispute.objects.get(task=self.task)
        # Should safely transition to pending_staff_review
        self.assertEqual(dispute.status, 'pending_staff_review')
        self.assertEqual(dispute.jurors.count(), 0)

    def test_stake_locking_and_ledger_entry(self):
        j1 = User.objects.create_user(username='j1', password='password123')
        UserProfile.objects.create(user=j1, rewards=500, is_phone_verified=True)
        j2 = User.objects.create_user(username='j2', password='password123')
        UserProfile.objects.create(user=j2, rewards=500, is_phone_verified=True)
        j3 = User.objects.create_user(username='j3', password='password123')
        UserProfile.objects.create(user=j3, rewards=500, is_phone_verified=True)

        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Scope creep'})

        for j in [j1, j2, j3]:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 450)
            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_held').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -50)

    def test_dispute_withdrawal_unlocks_juror_stakes(self):
        j1 = User.objects.create_user(username='j1', password='password123')
        UserProfile.objects.create(user=j1, rewards=500, is_phone_verified=True)
        j2 = User.objects.create_user(username='j2', password='password123')
        UserProfile.objects.create(user=j2, rewards=500, is_phone_verified=True)
        j3 = User.objects.create_user(username='j3', password='password123')
        UserProfile.objects.create(user=j3, rewards=500, is_phone_verified=True)

        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Scope creep'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.jurors.filter(is_stake_locked=True).count(), 3)

        # Withdraw dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        for j in [j1, j2, j3]:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 500)
            refund_ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_refunded').first()
            self.assertIsNotNone(refund_ledger)

    def test_juror_voting_resolution(self):
        jurors = []
        for i in range(1, 4):
            u = User.objects.create_user(username=f'juror_voter_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500, is_phone_verified=True)
            jurors.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Scope creep'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 and 2 vote for taken_by, Juror 3 votes for posted_by
        self.client.login(username='juror_voter_1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taken_by'})

        self.client.login(username='juror_voter_2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taken_by'})

        self.client.login(username='juror_voter_3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'posted_by'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Check winner (taken_by) rewards
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        # Check winning jurors (juror 1 and juror 2) balance: 450 + 50 (stake refund) + 25 (reward share) = 525
        for u in [jurors[0], jurors[1]]:
            u.userprofile.refresh_from_db()
            self.assertEqual(u.userprofile.rewards, 525)

