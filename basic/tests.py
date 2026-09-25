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


from .models import JurorAssignment, Friendship, Notification

class JurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task for testing juror selection",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_auto_juror_selection_on_dispute_creation(self):
        # Create 5 neutral community members
        community_members = []
        for i in range(5):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            community_members.append(u)

        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Verify exactly 3 juror assignments created
        assignments = JurorAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_user_ids = set(assignments.values_list('juror_id', flat=True))
        # Direct task creator and taker must not be assigned
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)

        # Verify notifications created for assigned jurors
        for juror_id in assigned_user_ids:
            notif = Notification.objects.filter(recipient_id=juror_id, link=reverse('dispute_detail', args=[dispute.id])).first()
            self.assertIsNotNone(notif)
            self.assertIn("assigned as a juror", notif.message)

    def test_social_graph_exclusion_friends_and_closeness(self):
        # Friend of poster
        friend_poster = User.objects.create_user(username='friend_poster', password='password123')
        fp_profile = UserProfile.objects.create(user=friend_poster, rewards=500)
        self.poster_profile.friends.add(fp_profile)

        # Friend of taker
        friend_taker = User.objects.create_user(username='friend_taker', password='password123')
        ft_profile = UserProfile.objects.create(user=friend_taker, rewards=500)
        self.taker_profile.friends.add(ft_profile)

        # High closeness with poster (closeness = 80)
        close_poster = User.objects.create_user(username='close_poster', password='password123')
        cp_profile = UserProfile.objects.create(user=close_poster, rewards=500)
        Friendship.objects.create(from_user=self.poster_profile, to_user=cp_profile, closeness=80)

        # High closeness with taker (closeness = 50)
        close_taker = User.objects.create_user(username='close_taker', password='password123')
        ct_profile = UserProfile.objects.create(user=close_taker, rewards=500)
        Friendship.objects.create(from_user=ct_profile, to_user=self.taker_profile, closeness=50)

        # Create 3 neutral users
        neutral1 = User.objects.create_user(username='neutral_1', password='password123')
        UserProfile.objects.create(user=neutral1, rewards=500)
        neutral2 = User.objects.create_user(username='neutral_2', password='password123')
        UserProfile.objects.create(user=neutral2, rewards=500)
        neutral3 = User.objects.create_user(username='neutral_3', password='password123')
        UserProfile.objects.create(user=neutral3, rewards=500)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assignments = JurorAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        assigned_user_ids = set(assignments.values_list('juror_id', flat=True))

        # None of the excluded users should be assigned
        excluded_ids = {
            self.poster.id,
            self.taker.id,
            friend_poster.id,
            friend_taker.id,
            close_poster.id,
            close_taker.id
        }
        for ex_id in excluded_ids:
            self.assertNotIn(ex_id, assigned_user_ids)

        # The 3 assigned jurors must be neutral1, neutral2, neutral3
        self.assertEqual(assigned_user_ids, {neutral1.id, neutral2.id, neutral3.id})

    def test_assigned_juror_view_access(self):
        # Create 3 neutral users for juror assignment
        for i in range(3):
            u = User.objects.create_user(username=f'juror_candidate_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'View access dispute'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [a.juror for a in dispute.juror_assignments.all()]
        self.assertEqual(len(assigned_jurors), 3)

        # Create a stranger user AFTER dispute was raised so stranger is not assigned as juror
        stranger = User.objects.create_user(username='stranger', password='password123')
        UserProfile.objects.create(user=stranger, rewards=500)

        # Stranger attempts to view dispute details
        self.client.login(username='stranger', password='password123')
        stranger_response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        # Access denied -> redirected to home
        self.assertRedirects(stranger_response, reverse('home'))

        # Assigned juror attempts to view dispute details
        assigned_juror = assigned_jurors[0]
        self.client.login(username=assigned_juror.username, password='password123')
        juror_response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(juror_response.status_code, 200)
        self.assertContains(juror_response, "Dispute Details")


