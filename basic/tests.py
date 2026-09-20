from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeJuror, Friendship, Notification, RewardLedger, Conversation


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


class JurorSelectionEngineTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, hostel='Hostel_A', batch=2026, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, hostel='Hostel_B', batch=2027, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Testing Juror Engine",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_automatic_juror_panel_selection(self):
        # Create 10 eligible community users
        candidates = []
        for i in range(10):
            u = User.objects.create_user(username=f'community_user_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel=f'Hostel_{i+10}', batch=2028, rewards=500)
            candidates.append(u)

        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work incomplete'}
        )

        dispute = Dispute.objects.get(task=self.task)
        jurors = dispute.jurors.all()
        self.assertEqual(jurors.count(), 5)
        self.assertFalse(dispute.flagged_for_staff)

        # Check notifications dispatched
        for juror in jurors:
            notification = Notification.objects.filter(recipient=juror).first()
            self.assertIsNotNone(notification)
            self.assertIn(f"dispute/{dispute.id}/", notification.link)

    def test_exclude_participants_and_friends(self):
        # Create friends for poster and taker
        friend_poster = User.objects.create_user(username='poster_friend', password='password123')
        fp_profile = UserProfile.objects.create(user=friend_poster, hostel='Hostel_C', batch=2028)
        self.poster_profile.friends.add(fp_profile)

        friend_taker = User.objects.create_user(username='taker_friend', password='password123')
        ft_profile = UserProfile.objects.create(user=friend_taker, hostel='Hostel_D', batch=2028)
        Friendship.objects.create(from_user=self.taker_profile, to_user=ft_profile)

        # Create 5 neutral candidates
        neutrals = []
        for i in range(5):
            u = User.objects.create_user(username=f'neutral_user_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel=f'Hostel_Z_{i}', batch=2029)
            neutrals.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_ids = set(dispute.jurors.values_list('id', flat=True))

        # Direct friends and participants must NEVER be assigned
        self.assertNotIn(self.poster.id, assigned_ids)
        self.assertNotIn(self.taker.id, assigned_ids)
        self.assertNotIn(friend_poster.id, assigned_ids)
        self.assertNotIn(friend_taker.id, assigned_ids)
        self.assertEqual(len(assigned_ids), 5)

    def test_cohort_peer_filtering_with_adequate_alternatives(self):
        # Poster cohort: Hostel_A, Batch 2026
        # Taker cohort: Hostel_B, Batch 2027
        # Create 5 non-peer candidates (Hostel_X, Batch 2028)
        non_peers = []
        for i in range(5):
            u = User.objects.create_user(username=f'non_peer_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel='Hostel_X', batch=2028)
            non_peers.append(u)

        # Create 5 cohort peers (Hostel_A, Batch 2026)
        peers = []
        for i in range(5):
            u = User.objects.create_user(username=f'cohort_peer_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel='Hostel_A', batch=2026)
            peers.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Cohort test'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_ids = set(dispute.jurors.values_list('id', flat=True))

        # Because 5 non-peers exist (adequate alternative pool), 0 cohort peers should be selected
        peer_ids = {u.id for u in peers}
        self.assertEqual(len(assigned_ids & peer_ids), 0)
        self.assertEqual(len(assigned_ids), 5)

    def test_cohort_peer_fallback_when_non_peers_inadequate(self):
        # Create 2 non-peer candidates
        non_peers = []
        for i in range(2):
            u = User.objects.create_user(username=f'sparse_non_peer_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel='Hostel_X', batch=2028)
            non_peers.append(u)

        # Create 5 cohort peers
        peers = []
        for i in range(5):
            u = User.objects.create_user(username=f'sparse_peer_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel='Hostel_A', batch=2026)
            peers.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Fallback test'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_ids = set(dispute.jurors.values_list('id', flat=True))

        # Panel size is 5 (2 non-peers + 3 cohort peers)
        self.assertEqual(len(assigned_ids), 5)
        self.assertFalse(dispute.flagged_for_staff)

    def test_exclude_users_in_active_disputes(self):
        # Create another open dispute where User ActiveDisputeParticipant is involved
        busy_user = User.objects.create_user(username='busy_user', password='password123')
        UserProfile.objects.create(user=busy_user, hostel='Hostel_M', batch=2028)

        other_task = Task.objects.create(
            title="Other Task",
            description="Desc",
            reward=200,
            posted_by=busy_user,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=other_task)
        Dispute.objects.create(
            task=other_task,
            raised_by=self.taker,
            reason='Other dispute',
            status='open'
        )

        # Create 5 neutral users
        neutrals = []
        for i in range(5):
            u = User.objects.create_user(username=f'active_neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel=f'Hostel_N_{i}', batch=2029)
            neutrals.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Active dispute check'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_ids = set(dispute.jurors.values_list('id', flat=True))
        self.assertNotIn(busy_user.id, assigned_ids)

    def test_juror_authorization_and_voting(self):
        # Create 5 neutral candidates
        candidates = []
        for i in range(5):
            u = User.objects.create_user(username=f'vote_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, hostel=f'Hostel_V_{i}', batch=2028)
            candidates.append(u)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Voting test'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_juror = dispute.jurors.first()

        # Create non-juror random user AFTER dispute selection
        outsider = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=outsider)

        # 1. Outsider access is forbidden
        self.client.login(username='outsider', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(resp, reverse('home'))

        # 2. Assigned juror access is granted
        self.client.login(username=assigned_juror.username, password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # 3. Assigned juror casts vote
        vote_resp = self.client.post(
            reverse('submit_dispute_vote', args=[dispute.id]),
            {'vote': 'poster'}
        )
        self.assertRedirects(vote_resp, reverse('dispute_detail', args=[dispute.id]))

        assignment = DisputeJuror.objects.get(dispute=dispute, user=assigned_juror)
        self.assertEqual(assignment.vote, 'poster')
        self.assertIsNotNone(assignment.voted_at)

        # 4. Duplicate vote attempt fails
        dup_resp = self.client.post(
            reverse('submit_dispute_vote', args=[dispute.id]),
            {'vote': 'taker'}
        )
        self.assertRedirects(dup_resp, reverse('dispute_detail', args=[dispute.id]))
        assignment.refresh_from_db()
        self.assertEqual(assignment.vote, 'poster')  # remains unchanged

    def test_staff_backup_flag_for_small_candidate_pool(self):
        # Only 1 candidate available in the system
        sole_candidate = User.objects.create_user(username='sole_candidate', password='password123')
        UserProfile.objects.create(user=sole_candidate, hostel='Hostel_Z', batch=2028)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Small pool test'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(dispute.flagged_for_staff)
        self.assertEqual(dispute.jurors.count(), 1)


