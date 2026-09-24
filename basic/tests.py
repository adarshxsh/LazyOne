from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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


class JurySelectionAndVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        # Poster and Taker
        self.poster = User.objects.create_user(username='poster_juror', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_juror', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Poster friend via UserProfile.friends
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # Taker friend via Friendship
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        from .models import Friendship, FriendRequest, JuryPool, JurorAssignment, Notification
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # Pending contact via FriendRequest
        self.pending_user = User.objects.create_user(username='pending_user', password='password123')
        self.pending_user_profile = UserProfile.objects.create(user=self.pending_user, rewards=500)
        FriendRequest.objects.create(from_user=self.poster, to_user=self.pending_user)

        # Neutral candidates
        self.neutral_jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'neutral_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            self.neutral_jurors.append(u)

        # Task
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Testing jury selection",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_coi_exclusion_and_juror_assignment(self):
        from .models import JuryPool, JurorAssignment, Notification
        self.client.login(username='taker_juror', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work disputed by taker'}
        )
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        jury_pool = JuryPool.objects.get(dispute=dispute)
        self.assertEqual(jury_pool.status, 'assigned')
        self.assertEqual(jury_pool.target_size, 3)

        assignments = jury_pool.assignments.all()
        assigned_user_ids = {a.juror.id for a in assignments}

        # Excluded users
        excluded_ids = {
            self.poster.id,
            self.taker.id,
            self.poster_friend.id,
            self.taker_friend.id,
            self.pending_user.id
        }

        # Verify 0 assigned jurors are in excluded set
        self.assertEqual(len(assigned_user_ids.intersection(excluded_ids)), 0)
        self.assertEqual(len(assigned_user_ids), 3)

        # Verify notifications sent to assigned jurors
        for juror in assignments:
            notif = Notification.objects.filter(recipient=juror.juror, link=reverse('dispute_detail', args=[dispute.id])).first()
            self.assertIsNotNone(notif)
            self.assertIn("peer juror", notif.message)

    def test_five_person_panel_selection(self):
        from .models import JuryPool
        self.client.login(username='taker_juror', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work disputed by taker', 'panel_size': '5'}
        )
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        jury_pool = JuryPool.objects.get(dispute=dispute)
        self.assertEqual(jury_pool.status, 'assigned')
        self.assertEqual(jury_pool.target_size, 5)
        self.assertEqual(jury_pool.assignments.count(), 5)

    def test_insufficient_jurors_handling(self):
        from .models import JuryPool, Notification
        # Create a new task where only 1 neutral user is available
        small_task = Task.objects.create(
            title="Small Task For Insufficient Jurors",
            description="Desc",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=small_task)

        # Deactivate all neutral jurors except 1
        for u in self.neutral_jurors[1:]:
            u.is_active = False
            u.save()

        self.client.login(username='taker_juror', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[small_task.id]),
            {'reason': 'Not enough jurors available'}
        )

        dispute = Dispute.objects.get(task=small_task)
        jury_pool = JuryPool.objects.get(dispute=dispute)
        self.assertEqual(jury_pool.status, 'insufficient_jurors')

        # Staff user notified
        staff_notif = Notification.objects.filter(recipient=self.staff_user, link=reverse('dispute_detail', args=[dispute.id])).first()
        self.assertIsNotNone(staff_notif)

    def test_dispute_detail_view_authorization(self):
        from .models import JuryPool, JurorAssignment
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Detail view auth test',
            deposit_amount=50,
            escrow_status='held'
        )
        jury_pool = JuryPool.objects.create(dispute=dispute, target_size=3, status='assigned')
        JurorAssignment.objects.create(jury_pool=jury_pool, juror=self.neutral_jurors[0])

        # Unauthorized neutral user
        self.client.login(username=self.neutral_jurors[1].username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Assigned juror CAN view detail
        self.client.login(username=self.neutral_jurors[0].username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Peer Jury Panel")

    def test_juror_majority_voting_worker_wins(self):
        from .models import JuryPool, JurorAssignment
        # Raise dispute
        self.client.login(username='taker_juror', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Disputed work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        jury_pool = dispute.jury_pool
        jurors = [a.juror for a in jury_pool.assignments.all()]

        # Juror 1 votes for worker
        self.client.login(username=jurors[0].username, password='password123')
        res1 = self.client.post(reverse('submit_juror_vote', args=[dispute.id]), {'vote': 'worker'})
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes for worker -> majority 2/3 reached!
        self.client.login(username=jurors[1].username, password='password123')
        res2 = self.client.post(reverse('submit_juror_vote', args=[dispute.id]), {'vote': 'worker'})
        self.assertRedirects(res2, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        jury_pool.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(jury_pool.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker balance: 500 - 50 (deposit) + 50 (deposit refund) + 200 (task reward) = 700
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

    def test_juror_majority_voting_poster_wins(self):
        from .models import JuryPool, JurorAssignment
        # Raise dispute
        self.client.login(username='taker_juror', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Disputed work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        jury_pool = dispute.jury_pool
        jurors = [a.juror for a in jury_pool.assignments.all()]

        # Juror 1 votes for poster
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('submit_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        # Juror 2 votes for poster -> majority 2/3 reached!
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('submit_juror_vote', args=[dispute.id]), {'vote': 'poster'})

        dispute.refresh_from_db()
        jury_pool.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'forfeited')
        self.assertEqual(jury_pool.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster balance: 1000 + 50 (forfeited deposit bond) = 1050
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1050)

