import hashlib
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote, Notification


class CommitRevealDisputeJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.j1 = User.objects.create_user(username='jury1', password='password123')
        self.j1_profile = UserProfile.objects.create(user=self.j1, rewards=200)

        self.j2 = User.objects.create_user(username='jury2', password='password123')
        self.j2_profile = UserProfile.objects.create(user=self.j2, rewards=200)

        self.j3 = User.objects.create_user(username='jury3', password='password123')
        self.j3_profile = UserProfile.objects.create(user=self.j3, rewards=200)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Jury Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfinished work dispute'})
        self.dispute = Dispute.objects.get(task=self.task)

    def test_dispute_parties_cannot_vote_as_jury(self):
        # Poster attempts to vote as jury
        self.client.login(username='poster', password='password123')
        res = self.client.post(
            reverse('commit_vote', args=[self.dispute.id]),
            {'vote': 'poster', 'salt': 'postersalt'}
        )
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Taker attempts to vote as jury
        self.client.login(username='taker', password='password123')
        res = self.client.post(
            reverse('commit_vote', args=[self.dispute.id]),
            {'vote': 'taker', 'salt': 'takersalt'}
        )
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_commit_phase_stores_only_hash_and_prevents_duplicates(self):
        self.client.login(username='jury1', password='password123')
        vote_choice = 'poster'
        salt = 'secret_passphrase_123'
        expected_hash = hashlib.sha256(f"{vote_choice}:{salt}".encode('utf-8')).hexdigest().lower()

        response = self.client.post(
            reverse('commit_vote', args=[self.dispute.id]),
            {'vote': vote_choice, 'salt': salt}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        vote = DisputeVote.objects.get(dispute=self.dispute, voter=self.j1)
        self.assertEqual(vote.commit_hash, expected_hash)
        self.assertFalse(vote.revealed)
        self.assertIsNone(vote.revealed_vote)

        # Duplicate commit attempt
        dup_response = self.client.post(
            reverse('commit_vote', args=[self.dispute.id]),
            {'vote': 'taker', 'salt': 'differentsalt'}
        )
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.j1).count(), 1)

    def test_reveal_phase_verification_failure_and_success(self):
        # Jury 1 commits
        self.client.login(username='jury1', password='password123')
        self.client.post(
            reverse('commit_vote', args=[self.dispute.id]),
            {'vote': 'taker', 'salt': 'mysalt99'}
        )

        # Advance to Reveal phase
        self.dispute.advance_to_reveal()
        self.assertEqual(self.dispute.status, 'reveal')

        # Try to reveal with WRONG salt -> should fail
        fail_res = self.client.post(
            reverse('reveal_vote', args=[self.dispute.id]),
            {'vote': 'taker', 'salt': 'wrongsalt'}
        )
        vote_obj = DisputeVote.objects.get(dispute=self.dispute, voter=self.j1)
        self.assertFalse(vote_obj.revealed)

        # Try to reveal with CORRECT salt & choice -> should succeed
        success_res = self.client.post(
            reverse('reveal_vote', args=[self.dispute.id]),
            {'vote': 'taker', 'salt': 'mysalt99'}
        )
        vote_obj.refresh_from_db()
        self.assertTrue(vote_obj.revealed)
        self.assertEqual(vote_obj.revealed_vote, 'taker')

    def test_full_commit_reveal_resolution_cycle_poster_wins(self):
        # Jury 1 votes poster
        self.client.login(username='jury1', password='password123')
        self.client.post(reverse('commit_vote', args=[self.dispute.id]), {'vote': 'poster', 'salt': 's1'})

        # Jury 2 votes poster
        self.client.login(username='jury2', password='password123')
        self.client.post(reverse('commit_vote', args=[self.dispute.id]), {'vote': 'poster', 'salt': 's2'})

        # Jury 3 votes taker
        self.client.login(username='jury3', password='password123')
        self.client.post(reverse('commit_vote', args=[self.dispute.id]), {'vote': 'taker', 'salt': 's3'})

        # Advance phase to reveal
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('advance_dispute_phase', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'reveal')

        # Reveals
        self.client.login(username='jury1', password='password123')
        self.client.post(reverse('reveal_vote', args=[self.dispute.id]), {'vote': 'poster', 'salt': 's1'})

        self.client.login(username='jury2', password='password123')
        self.client.post(reverse('reveal_vote', args=[self.dispute.id]), {'vote': 'poster', 'salt': 's2'})

        self.client.login(username='jury3', password='password123')
        self.client.post(reverse('reveal_vote', args=[self.dispute.id]), {'vote': 'taker', 'salt': 's3'})

        # Tally and resolve
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('advance_dispute_phase', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Poster wins:
        # Poster reward was 1000, receives task reward 200 + forfeited deposit bond 50 = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

        # Task cancelled
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Escrow status forfeited
        self.assertEqual(self.dispute.escrow_status, 'forfeited')

    def test_dispute_list_and_detail_views(self):
        self.client.login(username='jury1', password='password123')
        list_res = self.client.get(reverse('dispute_list'))
        self.assertEqual(list_res.status_code, 200)
        self.assertContains(list_res, self.task.title)

        detail_res = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(detail_res.status_code, 200)
        self.assertTrue(detail_res.context['can_vote'])
        self.assertFalse(detail_res.context['is_party'])



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

