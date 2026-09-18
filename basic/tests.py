from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse

from basic.models import (
    UserProfile, Task, Dispute, JuryAssignment, DisputeVote, RewardLedger, Friendship, Conversation
)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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
class SupermajorityDisputeAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.appeal_juror1 = User.objects.create_user(username='appeal_juror1', password='password123')
        self.appeal_juror2 = User.objects.create_user(username='appeal_juror2', password='password123')
        self.appeal_juror3 = User.objects.create_user(username='appeal_juror3', password='password123')

        # Initialize user profiles with rewards
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.j1_profile, _ = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 1500})
        self.j2_profile, _ = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 1500})
        self.j3_profile, _ = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 1500})
        self.aj1_profile, _ = UserProfile.objects.get_or_create(user=self.appeal_juror1, defaults={'rewards': 1500})
        self.aj2_profile, _ = UserProfile.objects.get_or_create(user=self.appeal_juror2, defaults={'rewards': 1500})
        self.aj3_profile, _ = UserProfile.objects.get_or_create(user=self.appeal_juror3, defaults={'rewards': 1500})

        # Create a task in progress
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_disinterested_juror_filtering(self):
        # Add a friend to poster and taker
        friend_user = User.objects.create_user(username='friend', password='password123')
        friend_profile, _ = UserProfile.objects.get_or_create(user=friend_user)
        self.poster_profile.friends.add(friend_profile)

        # Raise dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not acknowledged'}
        )
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = list(JuryAssignment.objects.filter(dispute=dispute, tier=1).values_list('juror_id', flat=True))

        # Poster, Taker, and Friend must not be in assigned jurors
        self.assertNotIn(self.poster.id, assigned_jurors)
        self.assertNotIn(self.taker.id, assigned_jurors)
        self.assertNotIn(friend_user.id, assigned_jurors)

    def test_supermajority_consensus_tier1_resolution(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair rejection'})
        dispute = Dispute.objects.get(task=self.task)

        # Explicitly assign juror1, juror2, juror3 for Tier 1
        JuryAssignment.objects.filter(dispute=dispute, tier=1).delete()
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, tier=1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror2, tier=1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror3, tier=1)

        # Juror 1 votes for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        dispute.refresh_from_db()

        # Juror 2 votes for taker (2 out of 3 = 66.7% >= 66% supermajority!)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.initial_winner, self.taker)
        self.assertIsNotNone(dispute.resolved_at)

        # Check task completion & rewards
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1600)  # 1500 + 100 reward

        # Verify RewardLedger entry
        payout_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_payout').first()
        self.assertIsNotNone(payout_ledger)

        # Check juror rewards for majority voters
        self.j1_profile.refresh_from_db()
        self.assertEqual(self.j1_profile.rewards, 1510)  # +10 juror reward

    def test_appeal_window_and_is_appealable(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved',
            resolved_at=timezone.now(),
            resolution_winner=self.taker,
            initial_winner=self.taker
        )
        self.assertTrue(dispute.is_appealable())

        # Set resolved_at to 49 hours ago
        dispute.resolved_at = timezone.now() - timedelta(hours=49)
        dispute.save()
        self.assertFalse(dispute.is_appealable())

    def test_appeal_submission_deducts_bond(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved',
            resolved_at=timezone.now(),
            resolution_winner=self.taker,
            initial_winner=self.taker
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('appeal_dispute', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

        dispute.refresh_from_db()
        self.assertIn(dispute.status, ['appealed', 'appeal_review'])
        self.assertEqual(dispute.appellant, self.poster)
        self.assertEqual(dispute.appeal_bond_amount, 100)

        # Verify bond deducted
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1400)  # 1500 - 100 bond

        # Verify RewardLedger appeal_fee
        fee_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_fee').first()
        self.assertIsNotNone(fee_ledger)
        self.assertEqual(fee_ledger.amount, -100)

    def test_appeal_overturn_slashing_and_bond_refund(self):
        # Initial dispute resolved in favor of taker (taker received 100 reward)
        self.taker_profile.rewards = 1600
        self.taker_profile.save()

        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved',
            resolved_at=timezone.now(),
            resolution_winner=self.taker,
            initial_winner=self.taker
        )
        # Record Tier 1 votes (juror1 voted for taker)
        DisputeVote.objects.create(dispute=dispute, voter=self.juror1, choice='taker', tier=1)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, tier=1)

        # Poster files appeal
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('appeal_dispute', args=[dispute.id]))
        dispute.refresh_from_db()

        # Assign Tier 2 jurors explicitly
        JuryAssignment.objects.filter(dispute=dispute, tier=2).delete()
        JuryAssignment.objects.create(dispute=dispute, juror=self.appeal_juror1, tier=2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.appeal_juror2, tier=2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.appeal_juror3, tier=2)

        # Tier 2 jurors vote 2-1 for poster (overturning initial ruling)
        self.client.login(username='appeal_juror1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        self.client.login(username='appeal_juror2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'slashed')
        self.assertEqual(dispute.resolution_winner, self.poster)

        # 1. Poster (appellant) gets appeal bond refunded (1400 -> 1500) + poster gets task reward refund (+100) = 1600
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1600)

        # 2. Taker (bad-actor initial winner) is reversed (-100) and slashed (-100): 1600 - 100 - 100 = 1400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1400)

        # Verify slash_penalty ledger for taker
        taker_slash = RewardLedger.objects.filter(user=self.taker, transaction_type='slash_penalty').first()
        self.assertIsNotNone(taker_slash)
        self.assertEqual(taker_slash.amount, -100)

        # 3. Dishonest Tier 1 juror (juror1) is slashed (1500 - 20 = 1480)
        self.j1_profile.refresh_from_db()
        self.assertEqual(self.j1_profile.rewards, 1480)

        juror_slash = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_slash').first()
        self.assertIsNotNone(juror_slash)

    def test_frivolous_appeal_upheld_and_slashing(self):
        # Initial dispute resolved in favor of taker
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test reason',
            status='resolved',
            resolved_at=timezone.now(),
            resolution_winner=self.taker,
            initial_winner=self.taker
        )

        # Poster files appeal (deducts 100 bond, rewards = 1400)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('appeal_dispute', args=[dispute.id]))
        dispute.refresh_from_db()

        # Assign Tier 2 jurors
        JuryAssignment.objects.filter(dispute=dispute, tier=2).delete()
        JuryAssignment.objects.create(dispute=dispute, juror=self.appeal_juror1, tier=2)
        JuryAssignment.objects.create(dispute=dispute, juror=self.appeal_juror2, tier=2)

        # Tier 2 votes uphold taker victory (frivolous appeal)
        self.client.login(username='appeal_juror1', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        self.client.login(username='appeal_juror2', password='password123')
        self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'slashed')

        # Poster forfeits bond (1400) and incurs slash penalty (-100) = 1300
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

        frivolous_slash = RewardLedger.objects.filter(user=self.poster, transaction_type='slash_penalty').first()
        self.assertIsNotNone(frivolous_slash)
