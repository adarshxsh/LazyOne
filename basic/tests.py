from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, FriendRequest, Friendship, Conversation
from basic.services import select_jurors_for_dispute, cast_juror_vote, resolve_dispute, expire_dispute


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
        self.assertEqual(dispute.status, 'voting_open')
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


class JurorPoolAndDisputeTestCase(TestCase):
    def setUp(self):
        # Create Poster and Worker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(
            user=self.poster, rewards=1000, is_phone_verified=True
        )

        self.worker = User.objects.create_user(username='worker', password='password123')
        self.worker_profile = UserProfile.objects.create(
            user=self.worker, rewards=1000, is_phone_verified=True
        )

        # Create Task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.worker,
            status='in_progress'
        )

        # Create Potential Jurors
        self.friend_of_poster = User.objects.create_user(username='poster_friend', password='password123')
        self.f_poster_prof = UserProfile.objects.create(
            user=self.friend_of_poster, rewards=500, is_phone_verified=True
        )
        self.poster_profile.friends.add(self.f_poster_prof)

        self.pending_worker_connection = User.objects.create_user(username='pending_worker_conn', password='password123')
        UserProfile.objects.create(
            user=self.pending_worker_connection, rewards=500, is_phone_verified=True
        )
        FriendRequest.objects.create(
            from_user=self.worker, to_user=self.pending_worker_connection, is_accepted=False
        )

        self.unverified_candidate = User.objects.create_user(username='unverified', password='password123')
        UserProfile.objects.create(
            user=self.unverified_candidate, rewards=500, is_phone_verified=False, is_instagram_verified=False
        )

        self.low_balance_candidate = User.objects.create_user(username='low_balance', password='password123')
        UserProfile.objects.create(
            user=self.low_balance_candidate, rewards=10, is_phone_verified=True
        )

        # Neutral Eligible Jurors
        self.neutral_jurors = []
        for i in range(1, 6):
            u = User.objects.create_user(username=f'neutral_juror_{i}', password='password123')
            UserProfile.objects.create(
                user=u, rewards=500, is_phone_verified=True
            )
            self.neutral_jurors.append(u)

    def test_juror_selection_anti_collusion_filters(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Incomplete work dispute',
            stake_amount=50,
            quorum=3
        )
        assigned = select_jurors_for_dispute(dispute, panel_size=3)

        self.assertEqual(len(assigned), 3)
        self.assertEqual(dispute.status, 'voting_open')

        assigned_ids = {j.id for j in assigned}

        # Check counterparties excluded
        self.assertNotIn(self.poster.id, assigned_ids)
        self.assertNotIn(self.worker.id, assigned_ids)

        # Check poster's direct friend excluded
        self.assertNotIn(self.friend_of_poster.id, assigned_ids)

        # Check worker's pending request connection excluded
        self.assertNotIn(self.pending_worker_connection.id, assigned_ids)

        # Check unverified candidate excluded
        self.assertNotIn(self.unverified_candidate.id, assigned_ids)

        # Check low balance candidate excluded
        self.assertNotIn(self.low_balance_candidate.id, assigned_ids)

        # All assigned jurors must be from neutral list
        for j in assigned:
            self.assertIn(j, self.neutral_jurors)

    def test_stake_lockup_and_voting(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Payment issue',
            stake_amount=50,
            quorum=3
        )
        select_jurors_for_dispute(dispute, panel_size=3)
        juror1 = dispute.jurors.all()[0]

        initial_rewards = juror1.userprofile.rewards

        # Cast vote
        cast_juror_vote(dispute, juror1, self.worker)

        juror1.userprofile.refresh_from_db()
        self.assertEqual(juror1.userprofile.rewards, initial_rewards - 50)

        # Ledger check
        ledger = RewardLedger.objects.filter(user=juror1, transaction_type='dispute_stake_lock').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -50)

        # Attempt duplicate vote
        with self.assertRaises(ValueError):
            cast_juror_vote(dispute, juror1, self.poster)

    def test_counterparties_cannot_vote(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Dispute test',
            stake_amount=50
        )
        dispute.jurors.set(self.neutral_jurors[:3])
        dispute.status = 'voting_open'
        dispute.save()

        with self.assertRaises(ValueError):
            cast_juror_vote(dispute, self.poster, self.worker)

        with self.assertRaises(ValueError):
            cast_juror_vote(dispute, self.worker, self.poster)

    def test_dispute_resolution_and_reward_redistribution(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Dispute test',
            stake_amount=50,
            quorum=3
        )
        select_jurors_for_dispute(dispute, panel_size=3)
        jurors = list(dispute.jurors.all())

        # Juror 1 and Juror 2 vote for worker (majority)
        cast_juror_vote(dispute, jurors[0], self.worker)
        cast_juror_vote(dispute, jurors[1], self.worker)

        # Juror 3 votes for poster (minority)
        cast_juror_vote(dispute, jurors[2], self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winner, self.worker)

        # Check task status updated to completed
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check minority voter (juror 3) lost stake (rewards = 500 - 50 = 450)
        jurors[2].userprofile.refresh_from_db()
        self.assertEqual(jurors[2].userprofile.rewards, 450)

        # Check majority voters received stake back (50) + pro-rata share of minority slashed stake (50 // 2 = 25)
        jurors[0].userprofile.refresh_from_db()
        jurors[1].userprofile.refresh_from_db()
        self.assertEqual(jurors[0].userprofile.rewards, 500 + 25)
        self.assertEqual(jurors[1].userprofile.rewards, 500 + 25)

    def test_expired_dispute_refunds_stakes(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Dispute test',
            stake_amount=50,
            quorum=3
        )
        select_jurors_for_dispute(dispute, panel_size=3)
        jurors = list(dispute.jurors.all())

        # Only 1 juror votes
        cast_juror_vote(dispute, jurors[0], self.worker)

        # Force expire dispute
        expire_dispute(dispute)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'expired')

        # Check juror 0 got stake refunded
        jurors[0].userprofile.refresh_from_db()
        self.assertEqual(jurors[0].userprofile.rewards, 500)

        refund_ledger = RewardLedger.objects.filter(user=jurors[0], transaction_type='dispute_stake_refund').first()
        self.assertIsNotNone(refund_ledger)

    def test_raise_dispute_view_triggers_juror_selection(self):
        client = Client()
        client.login(username='worker', password='password123')

        url = reverse('raise_dispute', args=[self.task.id])
        response = client.post(url, {'reason': 'Task disagreement'})

        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'voting_open')
        self.assertGreater(dispute.jurors.count(), 0)
