from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from .models import (
    UserProfile, Task, Dispute, JuryAssignment, DisputeVote,
    Conversation, Message, Notification, RewardLedger, Friendship
)


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
class PeerJuryArbitrationTests(TestCase):
    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, rewards=1000)

        # Create poster friend and taker friend
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile, _ = UserProfile.objects.get_or_create(user=self.poster_friend)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile, _ = UserProfile.objects.get_or_create(user=self.taker_friend)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # Create neutral community users
        self.neutral1 = User.objects.create_user(username='neutral1', password='password123')
        UserProfile.objects.get_or_create(user=self.neutral1)

        self.neutral2 = User.objects.create_user(username='neutral2', password='password123')
        UserProfile.objects.get_or_create(user=self.neutral2)

        self.neutral3 = User.objects.create_user(username='neutral3', password='password123')
        UserProfile.objects.get_or_create(user=self.neutral3)

        self.neutral4 = User.objects.create_user(username='neutral4', password='password123')
        UserProfile.objects.get_or_create(user=self.neutral4)

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff_user', password='password123', is_staff=True)
        UserProfile.objects.get_or_create(user=self.staff_user)

        # Create task and conversation
        self.task = Task.objects.create(
            title='Test Task for Arbitration',
            description='Do something lazy',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raising_dispute_generates_odd_jury_excluding_counterparties_and_friends(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not as requested'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)

        jury_members = list(User.objects.filter(jury_assignments__dispute=dispute))
        self.assertEqual(len(jury_members), 3)
        self.assertTrue(len(jury_members) % 2 == 1)

        # Exclude poster, taker, and their friends
        for member in jury_members:
            self.assertNotIn(member, [self.poster, self.taker, self.poster_friend, self.taker_friend])
            self.assertIn(member, [self.neutral1, self.neutral2, self.neutral3, self.neutral4, self.staff_user])

    def test_impaneled_juror_chat_read_only_access(self):
        # Raise dispute and impanel jurors
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed task')
        self.task.status = 'disputed'
        self.task.save()

        # Impanel neutral1, neutral2, neutral3
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral3)

        # Impaneled juror neutral1 accesses chat
        self.client.login(username='neutral1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Juror tries sending message -> Forbidden
        send_response = self.client.post(
            reverse('send_message', args=[self.conversation.id]),
            {'content': 'Hello from juror'}
        )
        self.assertEqual(send_response.status_code, 403)

    def test_unassigned_non_participant_blocked(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed task')
        self.task.status = 'disputed'
        self.task.save()

        # neutral1 is not impaneled
        self.client.login(username='neutral1', password='password123')
        chat_resp = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(chat_resp.status_code, 302)

        detail_resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(detail_resp.status_code, 302)

    def test_secret_voting_and_results_visibility(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed task')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral3)

        # Before voting, neutral1 cannot see results
        self.client.login(username='neutral1', password='password123')
        resp = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['can_vote'])
        self.assertFalse(resp.context['show_results'])

        # neutral1 votes poster
        vote_resp = self.client.post(
            reverse('submit_dispute_vote', args=[dispute.id]),
            {'choice': 'poster', 'reason': 'Poster is right'}
        )
        self.assertEqual(vote_resp.status_code, 302)

        # Now neutral1 can see results
        resp_after = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp_after.status_code, 200)
        self.assertTrue(resp_after.context['has_voted'])
        self.assertTrue(resp_after.context['show_results'])
        self.assertEqual(resp_after.context['poster_votes'], 1)

    def test_majority_consensus_payout_for_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed task')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral3)

        initial_poster_rewards = self.poster_profile.rewards

        # Vote 1: neutral1 votes poster
        self.client.login(username='neutral1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Vote 2: neutral2 votes poster -> Majority reached!
        self.client.login(username='neutral2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

    def test_majority_consensus_payout_for_taker(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed task')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral3)

        initial_taker_rewards = self.taker_profile.rewards

        # Vote 1 & 2 for taker
        self.client.login(username='neutral1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        self.client.login(username='neutral2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)

    def test_expired_voting_window_triggers_staff_escalation(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Expired dispute')
        dispute.created_at = timezone.now() - timedelta(hours=50)
        dispute.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.neutral1)

        self.client.login(username='neutral1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_expired'])

        # Staff notification created
        staff_notif = Notification.objects.filter(recipient=self.staff_user).first()
        self.assertIsNotNone(staff_notif)
        self.assertIn("expired without jury quorum", staff_notif.message)
