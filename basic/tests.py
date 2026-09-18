from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryAssignment, Friendship, FriendRequest, Notification


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


class JurorPoolSelectionAndConflictIsolationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create task poster and profile
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Create task taker and profile
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Create direct friend of poster via UserProfile.friends
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=1000)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # Create direct friend of taker via Friendship model
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=1000)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # Create eligible community members (3 neutral users)
        self.community_user1 = User.objects.create_user(username='comm_user1', password='password123')
        UserProfile.objects.create(user=self.community_user1, rewards=1000)

        self.community_user2 = User.objects.create_user(username='comm_user2', password='password123')
        UserProfile.objects.create(user=self.community_user2, rewards=1000)

        self.community_user3 = User.objects.create_user(username='comm_user3', password='password123')
        UserProfile.objects.create(user=self.community_user3, rewards=1000)

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        UserProfile.objects.create(user=self.staff_user, rewards=1000)

        # Create task
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task with dispute",
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    @override_settings(JUROR_PANEL_SIZE=3)
    def test_automated_juror_pool_selection_and_friend_exclusion(self):
        """
        Tests that upon dispute creation:
        - Task participants (poster, taker) and their direct friends are excluded from candidate pool.
        - Selected jurors are drawn strictly from eligible unbiased community members.
        """
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work delivered'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Verify 3 jurors assigned
        assignments = JuryAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_juror_ids = set(assignments.values_list('juror_id', flat=True))

        # Participants and friends MUST NOT be in assigned jurors
        self.assertNotIn(self.poster.id, assigned_juror_ids)
        self.assertNotIn(self.taker.id, assigned_juror_ids)
        self.assertNotIn(self.poster_friend.id, assigned_juror_ids)
        self.assertNotIn(self.taker_friend.id, assigned_juror_ids)
        self.assertNotIn(self.staff_user.id, assigned_juror_ids)

        # The 3 assigned jurors MUST be comm_user1, comm_user2, comm_user3
        expected_ids = {self.community_user1.id, self.community_user2.id, self.community_user3.id}
        self.assertEqual(assigned_juror_ids, expected_ids)

    @override_settings(JUROR_PANEL_SIZE=3)
    def test_disqualified_juror_and_third_party_access_control(self):
        """
        Tests access control on dispute_detail_view:
        - Task participants, staff, and assigned jurors CAN view dispute details.
        - Friends of participants and unselected third parties are DENIED access.
        """
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # 1. Taker (participant) access -> 200 OK
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # 2. Poster (participant) access -> 200 OK
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # 3. Staff access -> 200 OK
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # 4. Assigned juror access -> 200 OK
        self.client.login(username='comm_user1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # 5. Poster friend (disqualified third party) access -> DENIED (Redirected to home)
        self.client.login(username='poster_friend', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # 6. Unselected third party access -> DENIED (Redirected to home)
        unrelated_user = User.objects.create_user(username='unrelated', password='password123')
        UserProfile.objects.create(user=unrelated_user, rewards=1000)
        self.client.login(username='unrelated', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    @override_settings(JUROR_PANEL_SIZE=3)
    def test_low_candidate_edge_case_escalates_to_staff(self):
        """
        Tests that when there are fewer eligible candidates than required panel_size:
        - System logs warning and marks dispute as escalated to staff.
        - Zero jurors assigned.
        - Staff notification created.
        """
        # Delete 2 community members so only 1 eligible candidate exists (less than JUROR_PANEL_SIZE=3)
        self.community_user2.delete()
        self.community_user3.delete()

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Low candidate test'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Escalate to staff flag set
        self.assertTrue(dispute.is_escalated_to_staff)

        # Zero jurors assigned
        self.assertEqual(JuryAssignment.objects.filter(dispute=dispute).count(), 0)

        # Staff notification created
        staff_notification = Notification.objects.filter(recipient=self.staff_user).first()
        self.assertIsNotNone(staff_notification)
        self.assertIn("escalated to staff resolution", staff_notification.message)


