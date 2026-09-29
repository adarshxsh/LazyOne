import time
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import (
    UserProfile, Task, Dispute, RewardLedger, Conversation,
    Friendship, FriendRequest, DisputeJuror, DisputeVote, Notification
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


class JurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Litigants
        self.poster = User.objects.create_user(username='litigant_poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='litigant_taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Friends of poster
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # Friends of taker via Friendship model
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # User with pending friend request to poster
        self.pending_friend = User.objects.create_user(username='pending_friend', password='password123')
        UserProfile.objects.create(user=self.pending_friend, rewards=500)
        FriendRequest.objects.create(from_user=self.pending_friend, to_user=self.poster)

        # User with shared task history with taker
        self.task_history_user = User.objects.create_user(username='task_history_user', password='password123')
        UserProfile.objects.create(user=self.task_history_user, rewards=500)
        Task.objects.create(
            title="Old Task",
            description="History task",
            reward=100,
            posted_by=self.taker,
            taken_by=self.task_history_user,
            status='completed'
        )

        # Clean eligible community candidates (6 users to ensure >= 5 panel size)
        self.candidates = []
        for i in range(1, 7):
            u = User.objects.create_user(username=f'community_member_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            self.candidates.append(u)

        # Unassigned external user
        self.unassigned_user = User.objects.create_user(username='unassigned_stranger', password='password123')
        UserProfile.objects.create(user=self.unassigned_user, rewards=500)

        # Main active task
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task with dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_friend_isolated_juror_selection(self):
        self.client.login(username='litigant_taker', password='password123')
        
        start_time = time.time()
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work incomplete'}
        )
        dispute = Dispute.objects.get(task=self.task)
        elapsed_time = (time.time() - start_time) * 1000  # ms

        # Test selection speed directly
        dispute_temp = Dispute.objects.get(task=self.task)
        DisputeJuror.objects.filter(dispute=dispute_temp).delete() # clear assigned
        direct_start = time.time()
        from .views.dispute import select_and_assign_jurors
        select_and_assign_jurors(dispute_temp)
        direct_elapsed = (time.time() - direct_start) * 1000
        self.assertLess(direct_elapsed, 500, "Direct juror selection must take < 500ms")

        juror_assignments = DisputeJuror.objects.filter(dispute=dispute)

        # Must select 3 or 5 jurors
        self.assertIn(juror_assignments.count(), [3, 5])

        assigned_user_ids = set(juror_assignments.values_list('user_id', flat=True))

        # Check strict isolation: no litigants, no direct friends, no task history connections
        excluded_ids = {
            self.poster.id,
            self.taker.id,
            self.poster_friend.id,
            self.taker_friend.id,
            self.pending_friend.id,
            self.task_history_user.id
        }
        for ex_id in excluded_ids:
            self.assertNotIn(ex_id, assigned_user_ids)

        # Check notifications sent to all assigned jurors
        for dj in juror_assignments:
            notification = Notification.objects.filter(
                recipient=dj.user,
                link=reverse('dispute_detail', args=[dispute.id])
            ).first()
            self.assertIsNotNone(notification)

    def test_dispute_access_control(self):
        # Raise dispute
        self.client.login(username='litigant_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work incomplete'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Assigned juror can view dispute details
        assigned_juror = DisputeJuror.objects.filter(dispute=dispute).first().user
        self.client.login(username=assigned_juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Create user after dispute jury selection finished
        late_stranger = User.objects.create_user(username='late_stranger', password='password123')
        UserProfile.objects.create(user=late_stranger, rewards=500)

        # Unassigned non-staff user blocked from viewing
        self.client.login(username='late_stranger', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_confidential_voting_and_deliberation(self):
        self.client.login(username='litigant_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work incomplete'}
        )
        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [dj.user for dj in DisputeJuror.objects.filter(dispute=dispute)]
        self.assertEqual(len(assigned_jurors), 5)

        # 3 jurors vote for taker, 2 for poster
        # Juror 1 votes for taker
        self.client.login(username=assigned_jurors[0].username, password='password123')
        self.client.post(
            reverse('cast_dispute_vote', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )

        # Litigant checking during deliberation should not see vote breakdown
        self.client.login(username='litigant_poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertIsNone(response.context['poster_votes'])

        # Jurors 2 & 3 vote for taker
        for juror in assigned_jurors[1:3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('cast_dispute_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        # Jurors 4 & 5 vote for poster
        for juror in assigned_jurors[3:5]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('cast_dispute_vote', args=[dispute.id]),
                {'voted_for': self.poster.id}
            )

        # All 5 voted, dispute auto-resolved in favor of taker (3 votes to 2)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

    def test_staff_override_authority(self):
        self.client.login(username='litigant_taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work incomplete'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Staff user applies override
        staff_user = User.objects.create_superuser(username='admin', password='password123')
        self.client.login(username='admin', password='password123')

        response = self.client.post(
            reverse('staff_resolve_dispute', args=[dispute.id]),
            {'winner': self.poster.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'forfeited')

