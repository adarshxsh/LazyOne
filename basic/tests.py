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

    def test_complete_disputed_task_blocked(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster attempts to mark task as completed -> should be blocked
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')

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


from .models import JuryPool, JuryVote, Notification

class JuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create 4 neutral eligible jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=200)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=200)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=200)

        self.juror4 = User.objects.create_user(username='juror4', password='password123')
        UserProfile.objects.create(user=self.juror4, rewards=200)

        self.friend_user = User.objects.create_user(username='friend_user', password='password123')
        self.friend_profile = UserProfile.objects.create(user=self.friend_user, rewards=200)
        # Make friend_user a friend of poster
        self.poster_profile.friends.add(self.friend_profile)

        self.low_balance_user = User.objects.create_user(username='low_balance', password='password123')
        UserProfile.objects.create(user=self.low_balance_user, rewards=50)

        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_automatic_jury_selection_and_notifications(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        dispute = Dispute.objects.get(task=self.task)
        pool = JuryPool.objects.filter(dispute=dispute)

        # Should select exactly 3 jurors
        self.assertEqual(pool.count(), 3)
        juror_users = [jp.juror for jp in pool]

        # Poster, Taker, friend_user, and low_balance_user must NOT be in jury
        self.assertNotIn(self.poster, juror_users)
        self.assertNotIn(self.taker, juror_users)
        self.assertNotIn(self.friend_user, juror_users)
        self.assertNotIn(self.low_balance_user, juror_users)

        # Selected jurors must have notifications
        for juror in juror_users:
            self.assertTrue(
                Notification.objects.filter(recipient=juror, message__contains="peer juror").exists()
            )

    def test_voting_permissions_and_double_vote_prevention(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)

        pool = list(JuryPool.objects.filter(dispute=dispute))
        selected_juror = pool[0].juror

        # Non-juror attempt
        non_juror = self.friend_user
        self.client.login(username=non_juror.username, password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'poster'}
        )
        self.assertFalse(JuryVote.objects.filter(dispute=dispute, juror=non_juror).exists())

        # Disputant attempt
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'poster'}
        )
        self.assertFalse(JuryVote.objects.filter(dispute=dispute, juror=self.poster).exists())

        # Selected juror votes
        self.client.login(username=selected_juror.username, password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'poster'}
        )
        self.assertTrue(JuryVote.objects.filter(dispute=dispute, juror=selected_juror, vote='poster').exists())

        # Double vote attempt
        response = self.client.post(
            reverse('vote_dispute', args=[dispute.id]),
            {'vote': 'taker'}
        )
        self.assertEqual(JuryVote.objects.filter(dispute=dispute, juror=selected_juror).count(), 1)

    def test_majority_consensus_poster_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)
        jurors = [jp.juror for jp in JuryPool.objects.filter(dispute=dispute)]

        # Juror 1 votes poster
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes poster -> 2 of 3 votes = Majority!
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster gets task reward refund (200) + forfeited deposit bond from taker (50) = 1000 + 200 + 50 = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

    def test_majority_consensus_taker_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute'})
        dispute = Dispute.objects.get(task=self.task)
        jurors = [jp.juror for jp in JuryPool.objects.filter(dispute=dispute)]

        # Juror 1 votes taker
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taker'})

        # Juror 2 votes taker -> Majority!
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'vote': 'taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker gets task reward (200) + refunded deposit bond (50).
        # Initial taker rewards = 500. Deducted 50 when dispute raised -> 450.
        # 450 + 200 (reward) + 50 (deposit refund) = 700.
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)


