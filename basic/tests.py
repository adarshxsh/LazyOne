from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation


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


class CommunityArbitrationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create users and profiles
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 900})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        self.juror1_profile, _ = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 1000})
        self.juror2_profile, _ = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 1000})
        self.juror3_profile, _ = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 1000})

        # Create disputed task and conversation
        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=self.deadline,
            status='disputed'
        )

        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work completed but poster refuses to approve.'
        )

    def test_read_only_dispute_chat_access_for_non_participants(self):
        # Authenticated non-participant accesses dispute chat
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

    def test_non_disputed_private_chat_access_denied_for_non_participants(self):
        # Create non-disputed private task chat
        private_task = Task.objects.create(
            title='Private Task',
            description='Private Description',
            reward=50,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        private_conv = Conversation.objects.create(task=private_task)
        private_conv.participants.add(self.poster, self.taker)

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[private_conv.id]))
        self.assertEqual(response.status_code, 302) # Redirect to home with error

    def test_unauthenticated_user_redirected(self):
        # Unauthenticated user trying to access chat or dispute detail
        chat_res = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(chat_res.status_code, 302)
        self.assertIn('/login/', chat_res.url)

        detail_res = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(detail_res.status_code, 302)
        self.assertIn('/login/', detail_res.url)

    def test_direct_participants_barred_from_voting(self):
        # Task poster attempts to vote
        self.client.login(username='poster', password='password123')
        vote_res = self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'chosen_party': 'poster'})
        self.assertEqual(vote_res.status_code, 302)
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Task taker attempts to vote
        self.client.login(username='taker', password='password123')
        vote_res = self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'chosen_party': 'taker'})
        self.assertEqual(vote_res.status_code, 302)
        self.assertEqual(DisputeVote.objects.count(), 0)

    def test_duplicate_voting_prevented(self):
        self.client.login(username='juror1', password='password123')
        vote_url = reverse('cast_dispute_vote', args=[self.dispute.id])

        # First vote
        res1 = self.client.post(vote_url, {'chosen_party': 'taker'})
        self.assertEqual(res1.status_code, 302)
        self.assertEqual(DisputeVote.objects.count(), 1)

        # Duplicate vote attempt
        res2 = self.client.post(vote_url, {'chosen_party': 'poster'})
        self.assertEqual(res2.status_code, 302)
        self.assertEqual(DisputeVote.objects.count(), 1)

    def test_quorum_and_automated_verdict_resolution(self):
        vote_url = reverse('cast_dispute_vote', args=[self.dispute.id])

        # Juror 1 votes taker
        self.client.login(username='juror1', password='password123')
        self.client.post(vote_url, {'chosen_party': 'taker'})

        # Juror 2 votes poster
        self.client.login(username='juror2', password='password123')
        self.client.post(vote_url, {'chosen_party': 'poster'})

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open') # 2 votes, quorum target is 3

        # Juror 3 votes taker (3rd vote reaches quorum)
        self.client.login(username='juror3', password='password123')
        self.client.post(vote_url, {'chosen_party': 'taker'})

        # Check resolution
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1100) # 1000 + 100 reward
