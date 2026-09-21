from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryPool, JurorAssignment, Friendship, Notification
from .utils import select_juror_pool


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


class JurorPoolSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_social_graph_filtering_and_juror_selection(self):
        # Create users:
        # u1: 1st degree friend of poster
        u1 = User.objects.create_user(username='friend1_poster', password='password123')
        p1 = UserProfile.objects.create(user=u1)
        self.poster_profile.friends.add(p1)

        # u2: 2nd degree friend of poster (friend of u1)
        u2 = User.objects.create_user(username='friend2_poster', password='password123')
        p2 = UserProfile.objects.create(user=u2)
        p1.friends.add(p2)

        # u3: high closeness connection of taker (> 70)
        u3 = User.objects.create_user(username='close_taker', password='password123')
        p3 = UserProfile.objects.create(user=u3)
        Friendship.objects.create(from_user=self.taker_profile, to_user=p3, closeness=85)

        # u4, u5, u6, u7: neutral candidate users
        candidates = []
        for i in range(4, 8):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u)
            candidates.append(u)

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unclear requirements",
            deposit_amount=60,
            escrow_status='held'
        )

        jury_pool = select_juror_pool(dispute, num_jurors=3)
        assigned_jurors = [a.juror for a in jury_pool.assignments.all()]

        self.assertEqual(len(assigned_jurors), 3)

        # Exclusions check
        excluded_users = {self.poster, self.taker, u1, u2, u3}
        for juror in assigned_jurors:
            self.assertNotIn(juror, excluded_users)
            self.assertIn(juror, candidates)

    def test_graceful_fallback_when_candidate_pool_is_small(self):
        # u1: 1st degree friend of poster
        u1 = User.objects.create_user(username='f1_poster', password='password123')
        p1 = UserProfile.objects.create(user=u1)
        self.poster_profile.friends.add(p1)

        # u2: 2nd degree friend of poster
        u2 = User.objects.create_user(username='f2_poster', password='password123')
        p2 = UserProfile.objects.create(user=u2)
        p1.friends.add(p2)

        # u3: neutral user
        u3 = User.objects.create_user(username='neutral_3', password='password123')
        UserProfile.objects.create(user=u3)

        # Total users = poster, taker, u1 (1st deg), u2 (2nd deg), u3 (neutral).
        # Under full exclusions: poster, taker, u1, u2 excluded. Candidates = [u3] (len 1 < 3).
        # Fallback relaxes 2nd degree friend (u2), candidates = [u2, u3] (len 2).
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Reason",
            deposit_amount=60,
            escrow_status='held'
        )

        jury_pool = select_juror_pool(dispute, num_jurors=3)
        assigned_jurors = [a.juror for a in jury_pool.assignments.all()]

        # Must select available candidates up to max available (2)
        self.assertEqual(len(assigned_jurors), 2)
        # Direct parties (poster, taker) and 1st degree (u1) MUST STILL BE EXCLUDED
        strict_excluded = {self.poster, self.taker, u1}
        for juror in assigned_jurors:
            self.assertNotIn(juror, strict_excluded)

    def test_raise_dispute_automatically_assigns_juror_pool_and_notifies(self):
        # Create 5 neutral candidate users
        neutral_users = []
        for i in range(1, 6):
            u = User.objects.create_user(username=f'juror_cand_{i}', password='password123')
            UserProfile.objects.create(user=u)
            neutral_users.append(u)

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # JuryPool and assignments should be created
        self.assertTrue(hasattr(dispute, 'jury_pool'))
        assignments = JurorAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        for assignment in assignments:
            self.assertIn(assignment.juror, neutral_users)
            # Notification check
            notif = Notification.objects.filter(recipient=assignment.juror).first()
            self.assertIsNotNone(notif)
            self.assertIn('selected as a juror', notif.message)
            self.assertEqual(notif.link, reverse('dispute_detail', args=[dispute.id]))

    def test_dispute_detail_view_access_controls(self):
        # Neutral user candidate
        u1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=u1)

        unauthorized_user = User.objects.create_user(username='unauthorized', password='password123')
        UserProfile.objects.create(user=unauthorized_user)

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Test dispute",
            deposit_amount=60,
            escrow_status='held'
        )

        jury_pool = JuryPool.objects.create(dispute=dispute)
        JurorAssignment.objects.create(dispute=dispute, jury_pool=jury_pool, juror=u1)

        # Assigned juror can view dispute details
        self.client.login(username='juror1', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Task poster can view dispute details
        self.client.login(username='poster', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Task taker can view dispute details
        self.client.login(username='taker', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Unauthorized third party user is denied access
        self.client.login(username='unauthorized', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(resp, reverse('home'))


