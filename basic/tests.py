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


class JurorSelectionAndResolutionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Poster & Taker
        self.poster = User.objects.create_user(username='poster_juror_test', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000, hostel='North Hall', batch=2029)

        self.taker = User.objects.create_user(username='taker_juror_test', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000, hostel='South Hall', room_no='101')

        # 1st degree friend of poster
        self.friend_1st = User.objects.create_user(username='friend_1st', password='password123')
        self.friend_1st_profile = UserProfile.objects.create(user=self.friend_1st, rewards=1000)
        self.poster_profile.friends.add(self.friend_1st_profile)

        # 2nd degree friend of poster (friend of friend_1st)
        self.friend_2nd = User.objects.create_user(username='friend_2nd', password='password123')
        self.friend_2nd_profile = UserProfile.objects.create(user=self.friend_2nd, rewards=1000)
        self.friend_1st_profile.friends.add(self.friend_2nd_profile)

        # Hostel & Batch cohort match with poster
        self.cohort_poster = User.objects.create_user(username='cohort_poster', password='password123')
        self.cohort_poster_profile = UserProfile.objects.create(user=self.cohort_poster, rewards=1000, hostel='North Hall', batch=2029)

        # Hostel & Room_no cohort match with taker
        self.cohort_taker = User.objects.create_user(username='cohort_taker', password='password123')
        self.cohort_taker_profile = UserProfile.objects.create(user=self.cohort_taker, rewards=1000, hostel='South Hall', room_no='101')

        # 3 Clean Neutral Candidates
        self.neutral1 = User.objects.create_user(username='neutral1', password='password123')
        UserProfile.objects.create(user=self.neutral1, rewards=1000, hostel='East Hall', batch=2030)

        self.neutral2 = User.objects.create_user(username='neutral2', password='password123')
        UserProfile.objects.create(user=self.neutral2, rewards=1000, hostel='West Hall', batch=2031)

        self.neutral3 = User.objects.create_user(username='neutral3', password='password123')
        UserProfile.objects.create(user=self.neutral3, rewards=1000, hostel='Central Hall', batch=2032)

        # Task & Conversation
        self.task = Task.objects.create(
            title="Juror Test Task",
            description="Task Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_conflict_exclusion_filter(self):
        self.client.login(username='taker_juror_test', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality dispute'}
        )
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        assigned_user_ids = set(dispute.jurors.values_list('user_id', flat=True))

        # Check that 1st degree, 2nd degree, and cohort users were excluded
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)
        self.assertNotIn(self.friend_1st.id, assigned_user_ids)
        self.assertNotIn(self.friend_2nd.id, assigned_user_ids)
        self.assertNotIn(self.cohort_poster.id, assigned_user_ids)
        self.assertNotIn(self.cohort_taker.id, assigned_user_ids)

        # Check that the 3 neutral users were selected
        expected_neutrals = {self.neutral1.id, self.neutral2.id, self.neutral3.id}
        self.assertEqual(assigned_user_ids, expected_neutrals)

    def test_juror_notifications_and_chat_access(self):
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)

        # Verify notification sent to assigned juror with direct link
        from .models import Notification
        notif = Notification.objects.filter(recipient=self.neutral1).first()
        self.assertIsNotNone(notif)
        self.assertIn("assigned as a juror", notif.message)
        self.assertEqual(notif.link, reverse('dispute_detail', args=[dispute.id]))

        # Verify juror added to conversation participants
        self.task.conversation.refresh_from_db()
        self.assertIn(self.neutral1, self.task.conversation.participants.all())

        # Test juror authorization on dispute detail view
        self.client.login(username='neutral1', password='password123')
        res_detail = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res_detail.status_code, 200)

        # Test juror authorization on chat view
        res_chat = self.client.get(reverse('chat_view', args=[self.task.conversation.id]))
        self.assertEqual(res_chat.status_code, 200)

        # Test unauthorized non-juror access
        unauth_user = User.objects.create_user(username='unauth', password='password123')
        UserProfile.objects.create(user=unauth_user, rewards=1000)
        self.client.login(username='unauth', password='password123')
        res_unauth = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(res_unauth, reverse('home'))

    def test_juror_voting_and_majority_settlement(self):
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Quality dispute'})

        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes for taker
        self.client.login(username='neutral1', password='password123')
        res1 = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'vote': 'taker', 'reasoning': 'Taker followed instructions'}
        )
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))

        # Check Juror 1 vote recorded
        j1 = dispute.jurors.get(user=self.neutral1)
        self.assertEqual(j1.vote, 'taker')
        self.assertEqual(j1.reasoning, 'Taker followed instructions')

        # Try changing Juror 1 vote (should fail / be blocked)
        res_change = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'vote': 'poster', 'reasoning': 'Changed mind'}
        )
        j1.refresh_from_db()
        self.assertEqual(j1.vote, 'taker')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes for taker (reaches majority 2/3)
        self.client.login(username='neutral2', password='password123')
        self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'vote': 'taker', 'reasoning': 'Agreed with taker'}
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Verify audit entries in RewardLedger
        audit_records = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_deposit')
        self.assertTrue(audit_records.filter(description__icontains="Submitted juror vote").exists())
        self.assertTrue(audit_records.filter(description__icontains="Dispute resolved by majority juror vote").exists())

    def test_expired_juror_unassignment_and_replacement(self):
        self.client.login(username='taker_juror_test', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)

        # Create a new neutral candidate to act as replacement
        replacement_user = User.objects.create_user(username='replacement_neutral', password='password123')
        UserProfile.objects.create(user=replacement_user, rewards=1000, hostel='North-East Hall', batch=2033)

        # Expire neutral1's assignment (>24h ago)
        j1 = dispute.jurors.get(user=self.neutral1)
        from .models import DisputeJuror
        DisputeJuror.objects.filter(id=j1.id).update(assigned_at=timezone.now() - timedelta(hours=25))

        # Trigger check via detail view
        self.client.login(username='neutral2', password='password123')
        self.client.get(reverse('dispute_detail', args=[dispute.id]))

        # neutral1 should be unassigned and replaced by replacement_user
        self.assertFalse(dispute.jurors.filter(user=self.neutral1).exists())
        self.assertTrue(dispute.jurors.filter(user=replacement_user).exists())

    def test_staff_fallback_when_insufficient_candidates(self):
        # Create a task where all potential candidates are excluded
        isolated_poster = User.objects.create_user(username='isolated_poster', password='password123')
        UserProfile.objects.create(user=isolated_poster, rewards=1000)

        isolated_taker = User.objects.create_user(username='isolated_taker', password='password123')
        UserProfile.objects.create(user=isolated_taker, rewards=1000)

        task_isolated = Task.objects.create(
            title="Isolated Task",
            description="Isolated Description",
            reward=200,
            posted_by=isolated_poster,
            taken_by=isolated_taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=task_isolated)

        # Temporarily deactivate or exclude all other users
        User.objects.exclude(id__in=[isolated_poster.id, isolated_taker.id]).update(is_active=False)

        self.client.login(username='isolated_taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[task_isolated.id]),
            {'reason': 'No candidates available'}
        )

        dispute = Dispute.objects.get(task=task_isolated)
        self.assertEqual(dispute.jurors.count(), 0)


