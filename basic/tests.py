from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Potential jurors
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


class JurorSelectionAndStakeLockTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Friends of poster and taker
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Create Task
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task for juror selection test",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_automated_juror_selection_excludes_counterparties_and_friends(self):
        # Create neutral eligible jurors (rewards >= 50)
        neutral_jurors = []
        for i in range(1, 4):
            u = User.objects.create_user(username=f'neutral_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)
            neutral_jurors.append(u)

        # Raise dispute
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Counterparty dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        # Verify assigned jurors count
        assignments = JurorAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_users = [a.juror for a in assignments]
        
        # Verify counterparties and friends are NOT in assigned jurors
        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)
        self.assertNotIn(self.poster_friend, assigned_users)
        self.assertNotIn(self.taker_friend, assigned_users)

        # Verify all assigned users are neutral jurors
        for ju in assigned_users:
            self.assertIn(ju, neutral_jurors)
            ju.userprofile.refresh_from_db()
            self.assertEqual(ju.userprofile.rewards, 50) # 100 - 50 = 50

            # Verify RewardLedger entry
            ledger = RewardLedger.objects.filter(user=ju, transaction_type='juror_stake').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -50)

    def test_insufficient_rewards_jurors_excluded(self):
        # Create 2 eligible jurors (rewards 100) and 2 poor jurors (rewards 20)
        for i in range(1, 3):
            u = User.objects.create_user(username=f'rich_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)

        for i in range(1, 3):
            u = User.objects.create_user(username=f'poor_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=20)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Test poor jurors exclusion'}
        )

        dispute = Dispute.objects.get(task=self.task)
        # Total eligible candidates = 2 (< 3 required), so enters pending_jurors state
        self.assertEqual(dispute.status, 'pending_jurors')
        self.assertEqual(JurorAssignment.objects.filter(dispute=dispute).count(), 0)

    def test_juror_voting_resolution_stake_refund_and_slash(self):
        # Create 3 neutral jurors
        jurors = []
        for i in range(1, 4):
            u = User.objects.create_user(username=f'vote_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)
            jurors.append(u)

        # Taker raises dispute
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Voting test dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        # Juror 1 & Juror 2 vote 'taker', Juror 3 votes 'poster'
        assignments = list(JurorAssignment.objects.filter(dispute=dispute).order_by('id'))
        
        # Juror 1 votes 'taker'
        self.client.login(username=assignments[0].juror.username, password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'taker'})

        # Juror 2 votes 'taker'
        self.client.login(username=assignments[1].juror.username, password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'taker'})

        # Juror 3 votes 'poster'
        self.client.login(username=assignments[2].juror.username, password='password123')
        self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote_choice': 'poster'})

        # Dispute should now be resolved in favor of 'taker'
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Refresh juror assignments
        assignments[0].refresh_from_db()
        assignments[1].refresh_from_db()
        assignments[2].refresh_from_db()

        # Juror 1 & 2 (majority) released & refunded
        self.assertEqual(assignments[0].status, 'released')
        self.assertEqual(assignments[1].status, 'released')
        assignments[0].juror.userprofile.refresh_from_db()
        assignments[1].juror.userprofile.refresh_from_db()
        self.assertEqual(assignments[0].juror.userprofile.rewards, 100)
        self.assertEqual(assignments[1].juror.userprofile.rewards, 100)

        # Juror 3 (minority) slashed & stake NOT refunded
        self.assertEqual(assignments[2].status, 'slashed')
        assignments[2].juror.userprofile.refresh_from_db()
        self.assertEqual(assignments[2].juror.userprofile.rewards, 50)

    def test_dispute_withdrawal_releases_juror_stakes(self):
        # Create 3 neutral jurors
        for i in range(1, 4):
            u = User.objects.create_user(username=f'withdraw_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)

        # Taker raises dispute
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Withdrawal test dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(JurorAssignment.objects.filter(dispute=dispute).count(), 3)

        # Taker withdraws dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # All 3 assigned jurors should have status 'released' and rewards restored to 100
        for assignment in JurorAssignment.objects.filter(dispute=dispute):
            self.assertEqual(assignment.status, 'released')
            assignment.juror.userprofile.refresh_from_db()
            self.assertEqual(assignment.juror.userprofile.rewards, 100)


