from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote


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


class DisputeVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        UserProfile.objects.create(user=self.voter1, rewards=100)

        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        UserProfile.objects.create(user=self.voter2, rewards=100)

        self.voter3 = User.objects.create_user(username='voter3', password='password123')
        UserProfile.objects.create(user=self.voter3, rewards=100)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task undergoing dispute",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Quality dispute",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

    def test_uninvolved_user_can_view_dispute_detail(self):
        self.client.login(username='voter1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disputed Task")
        self.assertContains(response, "Quality dispute")

    def test_party_cannot_vote_on_own_dispute(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.poster.id},
            follow=True
        )
        self.assertContains(response, "cannot vote on your own dispute")
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute).exists())

    def test_uninvolved_user_can_vote(self):
        self.client.login(username='voter1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.taker.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1, voted_for=self.taker).exists())

    def test_double_voting_prevented(self):
        self.client.login(username='voter1', password='password123')
        self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.taker.id}
        )
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for': self.poster.id},
            follow=True
        )
        self.assertContains(response, "already voted")
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute).count(), 1)

    def test_quorum_majority_resolution_taker_wins(self):
        # Voter 1 votes for Taker
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.taker.id})

        # Voter 2 votes for Poster
        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.poster.id})

        # Dispute should still be open (2 votes < 3 quorum)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

        # Voter 3 votes for Taker (Quorum 3 reached, Taker wins 2-1)
        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.taker.id})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker raised dispute -> deposit 50 refunded, plus reward 200 awarded
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500 + 200 + 50)

    def test_quorum_majority_resolution_poster_wins(self):
        # Voter 1 votes for Poster
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.poster.id})

        # Voter 2 votes for Taker
        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.taker.id})

        # Voter 3 votes for Poster (Quorum 3 reached, Poster wins 2-1)
        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for': self.poster.id})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Taker raised dispute and lost -> deposit 50 forfeited to Poster
        # Poster gets task reward 200 refunded + 50 forfeited deposit
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000 + 200 + 50)


