from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment, Friendship, Notification


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


class JurorAssignmentAndSocialGraphTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster and taker
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Dispute Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_juror_assignment_on_dispute_creation(self):
        # Create 6 neutral candidate users
        neutral_users = []
        for i in range(1, 7):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            neutral_users.append(u)

        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assignments = JurorAssignment.objects.filter(dispute=dispute)

        # Criterion 1 & Requirement 3: 3 to 5 JurorAssignment entries generated
        self.assertGreaterEqual(assignments.count(), 3)
        self.assertLessEqual(assignments.count(), 5)

        for assignment in assignments:
            self.assertIn(assignment.user, neutral_users)
            self.assertNotEqual(assignment.user, self.poster)
            self.assertNotEqual(assignment.user, self.taker)
            self.assertEqual(assignment.status, 'assigned')

    def test_social_graph_conflict_exclusion(self):
        # 1. Friend of poster (M2M)
        friend_poster = User.objects.create_user(username='friend_poster', password='password123')
        friend_poster_profile = UserProfile.objects.create(user=friend_poster, rewards=500)
        self.poster_profile.friends.add(friend_poster_profile)

        # 2. Friend of taker (M2M)
        friend_taker = User.objects.create_user(username='friend_taker', password='password123')
        friend_taker_profile = UserProfile.objects.create(user=friend_taker, rewards=500)
        self.taker_profile.friends.add(friend_taker_profile)

        # 3. High closeness connection (> 30)
        high_close_user = User.objects.create_user(username='high_close_user', password='password123')
        high_close_profile = UserProfile.objects.create(user=high_close_user, rewards=500)
        Friendship.objects.create(from_user=self.poster_profile, to_user=high_close_profile, closeness=80)

        # 4. Neutral users
        neutral1 = User.objects.create_user(username='neutral_1', password='password123')
        UserProfile.objects.create(user=neutral1, rewards=500)

        neutral2 = User.objects.create_user(username='neutral_2', password='password123')
        UserProfile.objects.create(user=neutral2, rewards=500)

        neutral3 = User.objects.create_user(username='neutral_3', password='password123')
        UserProfile.objects.create(user=neutral3, rewards=500)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality issues'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_user_ids = set(JurorAssignment.objects.filter(dispute=dispute).values_list('user_id', flat=True))

        # Criterion 2: Conflicted users must NEVER be assigned
        self.assertNotIn(friend_poster.id, assigned_user_ids)
        self.assertNotIn(friend_taker.id, assigned_user_ids)
        self.assertNotIn(high_close_user.id, assigned_user_ids)
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)

        # Neutral users are assigned
        self.assertIn(neutral1.id, assigned_user_ids)
        self.assertIn(neutral2.id, assigned_user_ids)
        self.assertIn(neutral3.id, assigned_user_ids)

    def test_assigned_juror_access_dispute_detail(self):
        neutral_user = User.objects.create_user(username='assigned_juror', password='password123')
        UserProfile.objects.create(user=neutral_user, rewards=500)

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            escrow_status='held'
        )
        JurorAssignment.objects.create(dispute=dispute, user=neutral_user, status='assigned')

        # Criterion 3: Assigned juror can view dispute detail without authorization error
        self.client.login(username='assigned_juror', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_unassigned_third_party_restricted(self):
        third_party = User.objects.create_user(username='unassigned_user', password='password123')
        UserProfile.objects.create(user=third_party, rewards=500)

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            deposit_amount=50,
            escrow_status='held'
        )

        # Criterion 4: Unassigned third party receives error and redirect
        self.client.login(username='unassigned_user', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_selected_jurors_receive_notification(self):
        neutral_user = User.objects.create_user(username='notified_juror', password='password123')
        UserProfile.objects.create(user=neutral_user, rewards=500)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not done'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_juror = JurorAssignment.objects.filter(dispute=dispute, user=neutral_user).first()
        self.assertIsNotNone(assigned_juror)

        # Criterion 5: Selected juror receives system notification with link to dispute
        notification = Notification.objects.filter(recipient=neutral_user).first()
        self.assertIsNotNone(notification)
        self.assertIn("assigned as a juror for dispute", notification.message)
        self.assertEqual(notification.link, reverse('dispute_detail', args=[dispute.id]))


