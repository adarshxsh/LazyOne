from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Jury, Juror, Vote, Notification


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


class JuryVotingConsensusEngineTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # 3 Community jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=1000)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=1500)

        self.task = Task.objects.create(
            title="Design Logo",
            description="Create vector logo",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    def test_raise_dispute_assigns_jury_and_locks_stakes(self):
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster demands extra work not in spec'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertIsNotNone(dispute.jury)
        jury = dispute.jury
        self.assertEqual(jury.status, 'voting')
        self.assertEqual(jury.jurors.count(), 3)

        # Verify juror stakes deducted
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 950)  # 1000 - 50

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 450)   # 500 - 50

        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 1450)  # 1500 - 50

        # Check ledger
        stake_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake').first()
        self.assertIsNotNone(stake_ledger)
        self.assertEqual(stake_ledger.amount, -50)

    def test_weighted_consensus_aggregation_and_payouts(self):
        # Raise dispute
        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        jury = dispute.jury

        j1 = Juror.objects.get(jury=jury, user=self.juror1)
        j2 = Juror.objects.get(jury=jury, user=self.juror2)
        j3 = Juror.objects.get(jury=jury, user=self.juror3)

        # Set weights explicitly for test predictability
        j1.weight = 2.0
        j1.save()
        j2.weight = 2.0
        j2.save()
        j3.weight = 3.0
        j3.save()

        # Juror 1 votes Poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'poster'})

        # Juror 2 votes Poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'poster'})

        # Juror 3 votes Taker
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'taker'})

        jury.refresh_from_db()
        self.assertTrue(jury.consensus_reached)
        self.assertEqual(jury.consensus_outcome, 'poster_win')
        self.assertEqual(jury.status, 'resolved')

        # Task should be cancelled, poster gets reward back
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)  # 1000 original + 200 task reward refund + 50 forfeited deposit bond

        # Check juror rewards / slashing
        # Juror 3 lost -> slashed (gets 0 back)
        j3.refresh_from_db()
        self.assertEqual(j3.status, 'slashed')
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 1450)  # lost 50 stake

        # Juror 1 and 2 won -> each weight 2.0 (total winning weight 4.0).
        # Slashed pool = 50. Each gets 50 stake back + 25 reward bonus = 75 return.
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 1025)  # 950 + 75

        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 525)   # 450 + 75

    def test_supermajority_consensus_early_settlement(self):
        # Taker raises dispute
        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        jury = dispute.jury

        j1 = Juror.objects.get(jury=jury, user=self.juror1)
        j2 = Juror.objects.get(jury=jury, user=self.juror2)
        j3 = Juror.objects.get(jury=jury, user=self.juror3)
        j1.weight = 5.0
        j1.save()
        j2.weight = 2.0
        j2.save()
        j3.weight = 1.0
        j3.save()

        # Both vote Taker -> 4.0 / 4.0 = 100% >= 0.66 supermajority
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'taker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'taker'})

        jury.refresh_from_db()
        self.assertTrue(jury.consensus_reached)
        self.assertEqual(jury.consensus_outcome, 'taker_win')

        # Task completed
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker gets 500 + 200 (task reward) = 700 (deposit bond 50 was deducted when raised 500->450, then refunded +50 -> 700)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

    def test_non_juror_cannot_vote(self) :
        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)

        # Poster attempts to vote
        self.client.login(username='poster_user', password='password123')
        response = self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'poster'})

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(Vote.objects.filter(jury=dispute.jury).count(), 0)

    def test_juror_cannot_vote_twice(self):
        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'poster'})

        # Second vote attempt
        response = self.client.post(reverse('cast_vote', args=[dispute.id]), {'choice': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.assertEqual(Vote.objects.filter(jury=dispute.jury, juror__user=self.juror1).count(), 1)

    def test_dispute_withdrawal_refunds_juror_stakes(self):
        self.client.login(username='taker_user', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(self.juror1_profile.rewards, 1000)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 950)  # 50 stake deducted

        # Taker withdraws dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 1000)  # 50 stake refunded

        refund_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 50)


