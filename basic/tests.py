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


class DisputeChatAndVotingTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        # Create profiles
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter1, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter2, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1000})

        # Create task in progress
        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Description of task',
            reward=100,
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
            reason='Work was done properly but poster refuses to acknowledge.'
        )

    def test_community_member_can_view_disputed_chat_read_only(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])
        self.assertTrue(response.context['can_vote'])

    def test_non_participant_denied_send_message_in_disputed_chat(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello'})
        self.assertEqual(response.status_code, 403)

    def test_community_member_can_cast_vote(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1, voted_for=self.taker).exists())

    def test_task_parties_cannot_vote_on_own_dispute(self):
        client = Client()
        # Poster attempts to vote
        client.login(username='poster', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.poster.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Taker attempts to vote
        client.login(username='taker', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_voter_cannot_cast_multiple_votes(self):
        client = Client()
        client.login(username='voter1', password='password123')

        # First vote
        client.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for_id': self.taker.id})
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1).count(), 1)

        # Duplicate vote attempt
        client.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for_id': self.poster.id})
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1).count(), 1)

    def test_staff_view_vote_tallies(self):
        # Create votes
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter1, voted_for=self.taker)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter2, voted_for=self.poster)

        client = Client()

        # Non-staff user should NOT see vote tallies
        client.login(username='poster', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('show_vote_tallies', response.context)

        # Staff user DOES see vote tallies
        client.login(username='staff', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get('show_vote_tallies'))
        self.assertEqual(response.context['total_votes'], 2)
        self.assertEqual(response.context['taker_votes'], 1)
        self.assertEqual(response.context['poster_votes'], 1)

    def test_manual_staff_dispute_settlement(self):
        client = Client()

        # Non-staff settlement attempt blocked
        client.login(username='voter1', password='password123')
        response = client.post(reverse('settle_dispute', args=[self.dispute.id]), {'winner': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Dispute.objects.get(id=self.dispute.id).status, 'open')

        # Staff settlement in favor of taker
        client.login(username='staff', password='password123')
        taker_initial_rewards = self.taker.userprofile.rewards
        response = client.post(reverse('settle_dispute', args=[self.dispute.id]), {'winner': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker.userprofile.rewards, taker_initial_rewards + 100)
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=self.task, amount=100).exists())

