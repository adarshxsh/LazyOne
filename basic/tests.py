from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from basic.models import (
    UserProfile, Task, Dispute, JuryAssignment, DisputeVote,
    Notification, RewardLedger, Friendship, FriendRequest, Conversation
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JuryPanelArchitectureTests(TestCase):

    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})

        # Create 5 candidate community peers
        self.peers = []
        for i in range(1, 6):
            peer = User.objects.create_user(username=f'peer{i}', password='password123')
            UserProfile.objects.get_or_create(user=peer, defaults={'rewards': 1500})
            self.peers.append(peer)

        # Create an in_progress task and conversation
        self.task = Task.objects.create(
            title='Test Task for Dispute',
            description='Detailed task description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=2),
            status='in_progress'
        )
        self.conversation, _ = Conversation.objects.get_or_create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client = Client()

    def test_automatic_3_juror_panel_assembly_on_dispute_creation(self):
        """Test that raising a dispute forms a 3-juror panel excluding poster and taker."""
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work submitted but poster refused to complete.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = self.task.dispute
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check jury panel size
        assignments = JuryAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_jurors = set(assignments.values_list('user_id', flat=True))
        self.assertNotIn(self.poster.id, assigned_jurors)
        self.assertNotIn(self.taker.id, assigned_jurors)

        # Check notifications sent to selected jurors
        for juror_id in assigned_jurors:
            self.assertTrue(Notification.objects.filter(recipient_id=juror_id).exists())

        # Check notification sent to counterparty (poster)
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_exclusion_of_mutual_friends_from_jury_panel(self):
        """Test that friends of poster and taker are excluded from panel assignment."""
        # Make peer1 and peer2 friends of poster and taker
        poster_profile = self.poster.userprofile
        taker_profile = self.taker.userprofile

        poster_profile.friends.add(self.peers[0].userprofile)
        taker_profile.friends.add(self.peers[1].userprofile)

        self.client.login(username='poster', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Taker failed to deliver completed work.'}
        )

        dispute = self.task.dispute
        assigned_jurors = list(JuryAssignment.objects.filter(dispute=dispute).values_list('user_id', flat=True))

        self.assertNotIn(self.peers[0].id, assigned_jurors)
        self.assertNotIn(self.peers[1].id, assigned_jurors)
        self.assertIn(self.peers[2].id, assigned_jurors)
        self.assertIn(self.peers[3].id, assigned_jurors)
        self.assertIn(self.peers[4].id, assigned_jurors)

    def test_dispute_detail_access_control(self):
        """Test authorized vs unauthorized access to dispute details."""
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions.'}
        )
        dispute = self.task.dispute
        assigned_juror = JuryAssignment.objects.filter(dispute=dispute).first().user
        unassigned_peer = [p for p in self.peers if p != assigned_juror and not JuryAssignment.objects.filter(dispute=dispute, user=p).exists()][0]

        # Poster access -> Allowed
        self.client.login(username='poster', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Assigned Juror access -> Allowed
        self.client.login(username=assigned_juror.username, password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['can_vote'])

        # Unassigned peer access -> Denied & redirected to home
        self.client.login(username=unassigned_peer.username, password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

    def test_voting_restrictions_for_participants_and_unassigned_users(self):
        """Test that poster, taker, and unassigned users cannot vote."""
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason.'}
        )
        dispute = self.task.dispute

        # Poster attempt to vote -> Blocked
        self.client.login(username='poster', password='password123')
        resp = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.poster).exists())

        # Taker attempt to vote -> Blocked
        self.client.login(username='taker', password='password123')
        resp = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, voter=self.taker).exists())

    def test_single_confidential_voting_and_duplicate_prevention(self):
        """Test that an assigned juror can cast exactly one vote."""
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Reason for dispute.'}
        )
        dispute = self.task.dispute
        jurors = [assignment.user for assignment in dispute.jury_assignments.all()]
        juror1 = jurors[0]

        self.client.login(username=juror1.username, password='password123')

        # First vote -> Accepted
        resp = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, voter=juror1).count(), 1)

        # Second vote attempt -> Rejected
        resp = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, voter=juror1).count(), 1)

    def test_simple_majority_consensus_resolution_in_favor_of_taker(self):
        """Test simple majority (2 out of 3 votes) resolving in favor of taker."""
        initial_taker_rewards = self.taker.userprofile.rewards

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Reason for dispute.'}
        )
        dispute = self.task.dispute
        jurors = [assignment.user for assignment in dispute.jury_assignments.all()]

        # Juror 1 votes taker
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes taker -> Reaches 2/3 simple majority threshold!
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards + self.task.reward)

        # Check RewardLedger
        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, self.task.reward)

        # Check notifications sent for final verdict
        for juror in jurors:
            self.assertTrue(Notification.objects.filter(recipient=juror, message__contains='resolved in favor of taker').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster, message__contains='resolved in favor of taker').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker, message__contains='resolved in favor of taker').exists())

    def test_simple_majority_consensus_resolution_in_favor_of_poster(self):
        """Test simple majority (2 out of 3 votes) resolving in favor of poster."""
        initial_poster_rewards = self.poster.userprofile.rewards

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Reason for dispute.'}
        )
        dispute = self.task.dispute
        jurors = [assignment.user for assignment in dispute.jury_assignments.all()]

        # Juror 1 votes poster
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        # Juror 2 votes taker
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        # Juror 3 votes poster -> 2/3 simple majority for poster!
        self.client.login(username=jurors[2].username, password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster.userprofile.rewards, initial_poster_rewards + self.task.reward + dispute.deposit_amount)

        # Check RewardLedger
        ledger_entry = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, self.task.reward)
