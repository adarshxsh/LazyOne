from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation, Notification


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

class CommunityDisputeWayTests(TestCase):
    def setUp(self):
        # Create users & user profiles
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        self.voter3 = User.objects.create_user(username='voter3', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter1, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter2, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter3, defaults={'rewards': 1000})

        # Create task
        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )

        # Create conversation
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work was done but poster refuses to mark complete'
        )

    def test_non_participant_can_view_disputed_chat_in_readonly(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_readonly'])

    def test_non_participant_cannot_send_chat_message(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello!'})
        self.assertEqual(response.status_code, 403)

    def test_participant_and_non_participant_dispute_detail_access(self):
        client = Client()
        # Non participant viewing open dispute
        client.login(username='voter1', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_participant'])

    def test_participants_cannot_vote_on_own_dispute(self):
        client = Client()
        client.login(username='poster', password='password123')
        response = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'}, follow=True)
        self.assertEqual(DisputeVote.objects.count(), 0)
        self.assertContains(response, "cannot vote on your own dispute")

    def test_non_participant_voting_and_duplicate_prevention(self):
        client = Client()
        client.login(username='voter1', password='password123')

        # Vote 1
        response = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeVote.objects.count(), 1)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.taker_votes, 1)
        self.assertEqual(self.dispute.poster_votes, 0)

        # Duplicate Vote attempt
        response2 = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'}, follow=True)
        self.assertEqual(DisputeVote.objects.count(), 1)
        self.assertContains(response2, "already voted")

    def test_quorum_settlement_favoring_taker(self):
        # 3 votes cast: 2 for taker, 1 for poster
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_verdict, 'taker')
        self.assertEqual(self.task.status, 'completed')

        # Taker awarded 200 points (initial 1000 + 200 = 1200)
        taker_profile = UserProfile.objects.get(user=self.taker)
        self.assertEqual(taker_profile.rewards, 1200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications sent to both poster and taker
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.taker).count(), 1)

    def test_quorum_settlement_favoring_poster(self):
        # 3 votes cast: 2 for poster, 1 for taker
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_verdict, 'poster')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster refunded 200 points (initial 1000 + 200 = 1200)
        poster_profile = UserProfile.objects.get(user=self.poster)
        self.assertEqual(poster_profile.rewards, 1200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.taker).count(), 1)
