from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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
class JurorSelectionAndAdjudicationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users: poster, taker, candidate1, candidate2, candidate3, candidate4, friend1, friend2
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # 1st-degree friend of poster
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=1000)
        self.poster_profile.friends.add(self.poster_friend_profile)

        # 2nd-degree friend of poster (friend of poster_friend)
        self.poster_2nd_friend = User.objects.create_user(username='poster_2nd_friend', password='password123')
        self.poster_2nd_friend_profile = UserProfile.objects.create(user=self.poster_2nd_friend, rewards=1000)
        self.poster_friend_profile.friends.add(self.poster_2nd_friend_profile)

        # 4 eligible candidate jurors with positive rewards
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=1000)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=1000)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=1000)

        self.juror4 = User.objects.create_user(username='juror4', password='password123')
        UserProfile.objects.create(user=self.juror4, rewards=1000)

        # Task in progress
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

    def test_social_distance_filtering_and_juror_creation(self):
        # Taker raises a dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        from .models import DisputeJuror
        juror_records = DisputeJuror.objects.filter(dispute=dispute)

        # Requirement 1 & 2: 3 DisputeJuror records created upon opening dispute
        self.assertEqual(juror_records.count(), 3)

        assigned_user_ids = set(juror_records.values_list('user_id', flat=True))

        # Check social distance filtering (Criterion 1):
        # Excludes poster, taker, poster_friend (1st degree), poster_2nd_friend (2nd degree)
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)
        self.assertNotIn(self.poster_friend.id, assigned_user_ids)
        self.assertNotIn(self.poster_2nd_friend.id, assigned_user_ids)

        # Assigned jurors must be among the 4 eligible candidate jurors
        eligible_ids = {self.juror1.id, self.juror2.id, self.juror3.id, self.juror4.id}
        self.assertTrue(assigned_user_ids.issubset(eligible_ids))

    def test_view_permissions_for_assigned_jurors_and_unassigned_users(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = list(dispute.jurors.all())
        unassigned_user = [u for u in [self.juror1, self.juror2, self.juror3, self.juror4] if u not in assigned_jurors][0]

        # Criterion 3: Unassigned user cannot access dispute view
        self.client.login(username=unassigned_user.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

        # Criterion 3: Assigned juror CAN access dispute view
        assigned_juror = assigned_jurors[0]
        self.client.login(username=assigned_juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

    def test_majority_vote_triggers_automated_resolution(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = list(dispute.jurors.all())
        self.assertEqual(len(assigned_jurors), 3)

        juror_a = assigned_jurors[0]
        juror_b = assigned_jurors[1]

        # Juror A votes for poster
        self.client.login(username=juror_a.username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster', 'reasoning': 'Poster is right'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open') # 1 vote is not majority yet

        # Juror B votes for poster (reaches majority 2/3)
        self.client.login(username=juror_b.username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster', 'reasoning': 'Agree with poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Criterion 4: Automated resolution triggered
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled') # Poster win cancels task and refunds


