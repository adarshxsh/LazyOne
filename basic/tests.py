from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Friendship, JurorAssignment, DisputeVote, Notification


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DynamicJurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Poster's friend via UserProfile.friends
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # Taker's friend via Friendship model
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # 3 Neutral candidates
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=500)

        # Unrelated user
        self.outsider = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=self.outsider, rewards=500)

        # Task and conversation
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Project Task",
            description="Detailed Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_juror_selection_conflict_filtering(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)

        assignments = list(JurorAssignment.objects.filter(dispute=dispute))
        assigned_users = [a.juror for a in assignments]

        # Must assign an odd-numbered pool
        self.assertEqual(len(assignments) % 2, 1)

        # Exclude poster, taker, poster_friend, taker_friend
        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)
        self.assertNotIn(self.poster_friend, assigned_users)
        self.assertNotIn(self.taker_friend, assigned_users)

        # Assigned jurors should receive notifications
        for juror in assigned_users:
            self.assertTrue(Notification.objects.filter(recipient=juror).exists())

    def test_juror_view_and_chat_access_control(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        juror = dispute.jurors.first()

        # Assigned juror can view dispute details
        self.client.login(username=juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Assigned juror can view task chat in read-only mode
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Assigned juror cannot post chat messages
        post_resp = self.client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello'})
        self.assertEqual(post_resp.status_code, 403)

        # Outsider user (guaranteed unassigned) gets redirected when attempting access
        unassigned_user = User.objects.create_user(username='unassigned_user', password='password123')
        UserProfile.objects.create(user=unassigned_user, rewards=500)

        self.client.login(username='unassigned_user', password='password123')
        resp_dispute = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(resp_dispute, reverse('home'))

        resp_chat = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertRedirects(resp_chat, reverse('home'))

    def test_juror_voting_and_single_vote_enforcement(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        juror = dispute.jurors.first()

        self.client.login(username=juror.username, password='password123')

        # First vote succeeds
        response = self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'vote': 'taker', 'reasoning': 'Valid evidence'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertTrue(DisputeVote.objects.filter(dispute=dispute, juror=juror, vote='taker').exists())

        # Second vote is blocked
        response2 = self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'vote': 'poster', 'reasoning': 'Changing mind'}
        )
        self.assertRedirects(response2, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, juror=juror).count(), 1)

    def test_majority_consensus_automated_escrow_settlement(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Ensure exact 3 jurors assigned
        JurorAssignment.objects.filter(dispute=dispute).delete()
        JurorAssignment.objects.create(dispute=dispute, juror=self.juror1)
        JurorAssignment.objects.create(dispute=dispute, juror=self.juror2)
        JurorAssignment.objects.create(dispute=dispute, juror=self.juror3)

        # Juror 1 votes 'taker'
        self.client.login(username='juror1', password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'vote': 'taker', 'reasoning': 'Taker is right'}
        )
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes 'taker' -> majority reached (2/3)
        self.client.login(username='juror2', password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'vote': 'taker', 'reasoning': 'Agreed with taker'}
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check taker balance: 500 - 50 (deposit bond) + 200 (reward) + 50 (deposit bond refund) = 700
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        # Check voting jurors got micro-rewards (500 + 15 = 515)
        j1_profile = UserProfile.objects.get(user=self.juror1)
        j2_profile = UserProfile.objects.get(user=self.juror2)
        self.assertEqual(j1_profile.rewards, 515)
        self.assertEqual(j2_profile.rewards, 515)

        # Check ledger entry for micro-reward
        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 15)

