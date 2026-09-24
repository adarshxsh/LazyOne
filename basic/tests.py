from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryPool, JurorAssignment, Friendship, FriendRequest, Notification
from .jury import select_juror_pool, get_conflict_user_ids


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


class JurorSelectionAndStakeLockTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task participants
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Poster's direct friend (via UserProfile.friends)
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=200)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # Taker's direct friend (via Friendship model)
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=200)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # Taker's accepted friend request
        self.request_friend = User.objects.create_user(username='request_friend', password='password123')
        self.request_friend_profile = UserProfile.objects.create(user=self.request_friend, rewards=200)
        FriendRequest.objects.create(from_user=self.taker, to_user=self.request_friend, is_accepted=True)

        # Low rewards candidate (< 50 points)
        self.poor_user = User.objects.create_user(username='poor_user', password='password123')
        UserProfile.objects.create(user=self.poor_user, rewards=30)

        # 4 Neutral candidates with sufficient rewards (>= 50)
        self.neutrals = []
        for i in range(1, 5):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)
            self.neutrals.append(u)

        # Create Task & Dispute
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Juror Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair work demand",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

    def test_conflict_user_ids_detection(self):
        conflict_ids = get_conflict_user_ids(self.dispute)
        self.assertIn(self.poster.id, conflict_ids)
        self.assertIn(self.taker.id, conflict_ids)
        self.assertIn(self.poster_friend.id, conflict_ids)
        self.assertIn(self.taker_friend.id, conflict_ids)
        self.assertIn(self.request_friend.id, conflict_ids)

    def test_select_juror_pool_anti_bias_and_staking(self):
        jury_pool, jurors = select_juror_pool(self.dispute, pool_size=3, stake_amount=50)

        self.assertEqual(len(jurors), 3)
        self.assertEqual(jury_pool.status, 'active')

        # Ensure no conflict users or poor users were selected
        conflict_ids = {self.poster.id, self.taker.id, self.poster_friend.id, self.taker_friend.id, self.request_friend.id, self.poor_user.id}
        selected_ids = {j.id for j in jurors}
        self.assertTrue(selected_ids.isdisjoint(conflict_ids))

        # Check each selected juror's balance and ledger
        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 150) # 200 - 50 = 150

            ledger = RewardLedger.objects.filter(user=juror, transaction_type='juror_stake_lock').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -50)

            assignment = JurorAssignment.objects.filter(dispute=self.dispute, juror=juror).first()
            self.assertIsNotNone(assignment)
            self.assertEqual(assignment.stake_amount, 50)
            self.assertEqual(assignment.voting_status, 'assigned')

            notification = Notification.objects.filter(recipient=juror).first()
            self.assertIsNotNone(notification)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'voting')

    def test_fallback_when_insufficient_candidates(self):
        # Request pool size of 10 when only 4 neutral candidates exist
        jury_pool, jurors = select_juror_pool(self.dispute, pool_size=10, stake_amount=50)

        self.assertEqual(len(jurors), 0)
        self.assertEqual(jury_pool.status, 'fallback')

    def test_raise_dispute_triggers_juror_selection(self):
        new_task = Task.objects.create(
            title="Auto Jury Task",
            description="Auto Jury Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=new_task)

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[new_task.id]),
            {'reason': 'Auto jury dispute'}
        )

        new_dispute = Dispute.objects.get(task=new_task)
        self.assertEqual(new_dispute.status, 'voting')
        self.assertTrue(hasattr(new_dispute, 'jury_pool'))
        self.assertEqual(new_dispute.juror_assignments.count(), 3)

    def test_juror_authorization_for_dispute_detail(self):
        jury_pool, jurors = select_juror_pool(self.dispute, pool_size=3, stake_amount=50)
        assigned_juror = jurors[0]

        # Assigned juror can view dispute detail
        self.client.login(username=assigned_juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Unassigned non-participant cannot view dispute detail
        unassigned_user = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=unassigned_user, rewards=100)
        self.client.login(username='outsider', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))


