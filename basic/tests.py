from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Friendship, JurorAssignment
from .juror_service import get_eligible_juror_candidates, assign_jurors_to_dispute, submit_juror_vote


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


class NeutralJurorPoolTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Litigants
        self.poster = User.objects.create_user(username='poster_user', password='password123', last_login=timezone.now())
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123', last_login=timezone.now())
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Poster's friend (via ManyToMany)
        self.poster_friend_m2m = User.objects.create_user(username='poster_friend_m2m', password='password123', last_login=timezone.now())
        self.poster_friend_m2m_profile = UserProfile.objects.create(user=self.poster_friend_m2m, rewards=100)
        self.poster_profile.friends.add(self.poster_friend_m2m_profile)

        # Taker's friend (via Friendship model)
        self.taker_friend_fs = User.objects.create_user(username='taker_friend_fs', password='password123', last_login=timezone.now())
        self.taker_friend_fs_profile = UserProfile.objects.create(user=self.taker_friend_fs, rewards=100)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_fs_profile)

        # Inactive user (logged in 40 days ago)
        self.stale_user = User.objects.create_user(
            username='stale_user',
            password='password123',
            last_login=timezone.now() - timedelta(days=40)
        )
        UserProfile.objects.create(user=self.stale_user, rewards=100)

        # Low rewards balance user (< 50)
        self.low_rewards_user = User.objects.create_user(username='low_rewards_user', password='password123', last_login=timezone.now())
        UserProfile.objects.create(user=self.low_rewards_user, rewards=30)

        # Neutral eligible candidates
        self.neutral1 = User.objects.create_user(username='neutral1', password='password123', last_login=timezone.now())
        UserProfile.objects.create(user=self.neutral1, rewards=100)

        self.neutral2 = User.objects.create_user(username='neutral2', password='password123', last_login=timezone.now())
        UserProfile.objects.create(user=self.neutral2, rewards=100)

        self.neutral3 = User.objects.create_user(username='neutral3', password='password123', last_login=timezone.now())
        UserProfile.objects.create(user=self.neutral3, rewards=100)

        # Task and Dispute
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work incomplete",
            deposit_amount=50,
            escrow_status='held'
        )

    def test_eligible_juror_candidate_filtering(self):
        eligible_candidates = get_eligible_juror_candidates(self.dispute)
        eligible_usernames = set(eligible_candidates.values_list('username', flat=True))

        # Check litigants excluded
        self.assertNotIn('poster_user', eligible_usernames)
        self.assertNotIn('taker_user', eligible_usernames)

        # Check friends of poster and taker excluded
        self.assertNotIn('poster_friend_m2m', eligible_usernames)
        self.assertNotIn('taker_friend_fs', eligible_usernames)

        # Check inactive/stale user (> 30 days) excluded
        self.assertNotIn('stale_user', eligible_usernames)

        # Check low rewards balance (< 50) user excluded
        self.assertNotIn('low_rewards_user', eligible_usernames)

        # Check neutrals are present
        self.assertIn('neutral1', eligible_usernames)
        self.assertIn('neutral2', eligible_usernames)
        self.assertIn('neutral3', eligible_usernames)

    def test_juror_stake_lock_on_panel_assignment(self):
        assignments = assign_jurors_to_dispute(self.dispute, panel_size=3, stake_amount=10)
        self.assertEqual(len(assignments), 3)

        assigned_jurors = [a.juror for a in assignments]
        for juror in assigned_jurors:
            # Rewards balance deducted by 10 (100 - 10 = 90)
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 90)

            # RewardLedger recorded
            ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_stake').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -10)

            # Assignment held
            assignment = JurorAssignment.objects.get(dispute=self.dispute, juror=juror)
            self.assertEqual(assignment.stake_status, 'held')
            self.assertFalse(assignment.voted)

    def test_juror_vote_submission_and_stake_refund(self):
        assignments = assign_jurors_to_dispute(self.dispute, panel_size=1, stake_amount=10)
        assignment = assignments[0]
        juror = assignment.juror

        # Initial rewards after assignment stake lock
        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, 90)

        # Submit vote for poster
        updated_assignment = submit_juror_vote(assignment, voted_for=self.poster)

        # Vote recorded
        self.assertTrue(updated_assignment.voted)
        self.assertEqual(updated_assignment.voted_for, self.poster)
        self.assertEqual(updated_assignment.stake_status, 'refunded')

        # Reward refunded (90 + 10 = 100)
        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, 100)

        # RewardLedger entry created
        ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_stake_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 10)

    def test_vote_submission_via_endpoint(self):
        # Assign juror
        assignments = assign_jurors_to_dispute(self.dispute, panel_size=1, stake_amount=10)
        assignment = assignments[0]
        juror = assignment.juror

        self.client.login(username=juror.username, password='password123')
        response = self.client.post(
            reverse('submit_dispute_vote', args=[self.dispute.id]),
            {'voted_for': self.poster.id}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        assignment.refresh_from_db()
        self.assertTrue(assignment.voted)
        self.assertEqual(assignment.voted_for, self.poster)
        self.assertEqual(assignment.stake_status, 'refunded')

        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, 100)

