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
class JurorSelectionAndVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster_juror_test', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_juror_test', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Direct friend of poster
        self.friend_poster = User.objects.create_user(username='friend_poster', password='password123')
        self.friend_poster_profile = UserProfile.objects.create(user=self.friend_poster)
        self.poster_profile.friends.add(self.friend_poster_profile)

        # Direct friend of taker
        self.friend_taker = User.objects.create_user(username='friend_taker', password='password123')
        self.friend_taker_profile = UserProfile.objects.create(user=self.friend_taker)
        self.taker_profile.friends.add(self.friend_taker_profile)

        # 1-hop mutual connection (friend of friend_poster)
        self.mutual_friend = User.objects.create_user(username='mutual_friend', password='password123')
        self.mutual_friend_profile = UserProfile.objects.create(user=self.mutual_friend)
        self.friend_poster_profile.friends.add(self.mutual_friend_profile)

        # Neutral eligible candidate users (j1, j2, j3, j4)
        self.juror_candidates = []
        for i in range(1, 6):
            u = User.objects.create_user(username=f'neutral_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            self.juror_candidates.append(u)

        # Create active task
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Juror Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_get_exclusion_set(self):
        from basic.services.juror_selection import ORMFilteredJurorSelectionService
        exclusion_set = ORMFilteredJurorSelectionService.get_exclusion_set(self.task)

        # Disputants excluded
        self.assertIn(self.poster.id, exclusion_set)
        self.assertIn(self.taker.id, exclusion_set)

        # 1st degree direct friends excluded
        self.assertIn(self.friend_poster.id, exclusion_set)
        self.assertIn(self.friend_taker.id, exclusion_set)

        # 1-hop mutual connection excluded
        self.assertIn(self.mutual_friend.id, exclusion_set)

        # Neutral candidates should NOT be in exclusion set
        for candidate in self.juror_candidates:
            self.assertNotIn(candidate.id, exclusion_set)

    def test_raise_dispute_selects_and_assigns_jurors(self):
        self.client.login(username='taker_juror_test', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work disagreement'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.juror_pool_status, 'assigned')
        self.assertEqual(dispute.jurors.count(), 3)

        # Check assigned jurors are unbiased
        assigned_user_ids = set(dispute.jurors.values_list('user_id', flat=True))
        from basic.services.juror_selection import ORMFilteredJurorSelectionService
        exclusion_set = ORMFilteredJurorSelectionService.get_exclusion_set(dispute)
        for u_id in assigned_user_ids:
            self.assertNotIn(u_id, exclusion_set)

    def test_cast_juror_vote_and_automated_settlement(self):
        # Raise dispute
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work disagreement'}
        )
        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = list(dispute.jurors.all())
        self.assertEqual(len(assigned_jurors), 3)

        j1 = assigned_jurors[0].user
        j2 = assigned_jurors[1].user

        # Juror 1 votes for poster
        self.client.login(username=j1.username, password='password123')
        res1 = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'posted_by'})
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open') # 1 vote is not majority

        # Juror 2 votes for poster -> majority (2/3) reached
        self.client.login(username=j2.username, password='password123')
        res2 = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'posted_by'})
        self.assertRedirects(res2, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.verdict, 'posted_by')
        self.assertEqual(dispute.juror_pool_status, 'completed')
        self.assertEqual(self.task.status, 'cancelled')

    def test_unauthorized_user_cannot_access_or_vote(self):
        # Raise dispute
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work disagreement'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Create an outsider user not involved, not a friend, and not chosen as juror
        outsider = User.objects.create_user(username='outsider_user', password='password123')
        UserProfile.objects.create(user=outsider)

        # Login as outsider
        self.client.login(username='outsider_user', password='password123')

        # Try viewing dispute
        res_view = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(res_view, reverse('home'))

        # Try voting
        res_vote = self.client.post(reverse('cast_juror_vote', args=[dispute.id]), {'vote': 'posted_by'})
        self.assertRedirects(res_vote, reverse('dispute_detail', args=[dispute.id]), fetch_redirect_response=False)

    def test_juror_selection_latency(self):
        import time
        from basic.services.juror_selection import ORMFilteredJurorSelectionService

        start_time = time.perf_counter()
        exclusion_set = ORMFilteredJurorSelectionService.get_exclusion_set(self.task)
        candidates = list(ORMFilteredJurorSelectionService.get_eligible_candidates(self.task))
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        self.assertLess(elapsed_ms, 15.0)


