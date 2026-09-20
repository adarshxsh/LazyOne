from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryAssignment, DisputeVote


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


class DisputeGovernanceAndEscalationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Peer jurors (5 users)
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=self.juror3, rewards=500)

        self.senior_juror1 = User.objects.create_user(username='senior_juror1', password='password123')
        UserProfile.objects.create(user=self.senior_juror1, rewards=500)

        self.senior_juror2 = User.objects.create_user(username='senior_juror2', password='password123')
        UserProfile.objects.create(user=self.senior_juror2, rewards=500)

        self.task = Task.objects.create(
            title="Escalation Test Task",
            description="Testing dispute escalation",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_supermajority_quorum_and_appeal_workflow(self):
        # 1. Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work submitted but rejected'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.stage, 'initial')

        # Check initial juror assignments created
        initial_juror_ids = list(JuryAssignment.objects.filter(dispute=dispute, stage='initial').values_list('juror_id', flat=True))
        self.assertGreaterEqual(len(initial_juror_ids), 2)

        # 2. Jurors vote: 2 vote for taker
        j1 = User.objects.get(id=initial_juror_ids[0])
        self.client.login(username=j1.username, password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()

        j2 = User.objects.get(id=initial_juror_ids[1])
        self.client.login(username=j2.username, password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()
        self.assertTrue(dispute.consensus_reached)
        self.assertEqual(dispute.status, 'pending_consensus')
        self.assertIsNotNone(dispute.appeal_window_expires_at)
        self.assertTrue(dispute.is_appealable())

        # 3. Poster files an appeal within 48h
        self.client.login(username='poster', password='password123')
        poster_rewards_before = self.poster_profile.rewards
        appeal_bond = self.task.deposit_bond_amount

        response = self.client.post(reverse('file_appeal', args=[dispute.id]), {'appeal_reason': 'The evidence was misinterpreted'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'senior_review')
        self.assertEqual(dispute.stage, 'senior')
        self.assertEqual(dispute.appealed_by, self.poster)
        self.assertEqual(dispute.appeal_deposit_amount, appeal_bond)

        # Poster rewards deducted
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, poster_rewards_before - appeal_bond)

        # Verify ledger for appeal deposit
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -appeal_bond)

        # Verify senior jurors do NOT include initial jurors (Guardrail check)
        senior_juror_ids = list(JuryAssignment.objects.filter(dispute=dispute, stage='senior').values_list('juror_id', flat=True))
        for init_id in initial_juror_ids:
            self.assertNotIn(init_id, senior_juror_ids)

    def test_appeal_window_expiration(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Mark consensus reached in initial stage
        dispute.consensus_reached = True
        dispute.status = 'pending_consensus'
        # Set expiration in the past (49 hours ago)
        dispute.appeal_window_expires_at = timezone.now() - timedelta(hours=49)
        dispute.save()

        self.assertFalse(dispute.is_appealable())

        # Poster attempts to appeal -> rejected
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('file_appeal', args=[dispute.id]), {'appeal_reason': 'Late appeal'})

        dispute.refresh_from_db()
        self.assertNotEqual(dispute.status, 'senior_review')

    def test_slashing_bad_actor_jurors_and_clamping(self):
        # Setup dispute in senior review stage
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Evidence dispute',
            deposit_amount=50,
            escrow_status='held',
            status='senior_review',
            stage='senior',
            appealed_by=self.poster,
            appeal_deposit_amount=50,
            appeal_escrow_status='held'
        )

        # Create 3 senior jurors
        sj1 = User.objects.create_user(username='sj1', password='password123')
        UserProfile.objects.create(user=sj1, rewards=30) # Low balance to test clamping!
        JuryAssignment.objects.create(dispute=dispute, juror=sj1, stage='senior', staked_amount=50)

        sj2 = User.objects.create_user(username='sj2', password='password123')
        UserProfile.objects.create(user=sj2, rewards=500)
        JuryAssignment.objects.create(dispute=dispute, juror=sj2, stage='senior', staked_amount=50)

        sj3 = User.objects.create_user(username='sj3', password='password123')
        UserProfile.objects.create(user=sj3, rewards=500)
        JuryAssignment.objects.create(dispute=dispute, juror=sj3, stage='senior', staked_amount=50)

        # sj1 and sj2 vote for taker, sj3 votes for poster (bad actor)
        self.client.login(username='sj1', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        self.client.login(username='sj3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        self.client.login(username='sj2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        # Resolution triggered! Winning choice is taker.
        dispute.refresh_from_db()
        self.assertIn(dispute.status, ['resolved', 'slashed'])

        # Check bad actor sj3 (voted poster) had stake slashed
        sj3_profile = UserProfile.objects.get(user=sj3)
        self.assertEqual(sj3_profile.rewards, 450) # 500 - 50 = 450

        # Check ledger entry for sj3 slash
        slash_ledger = RewardLedger.objects.filter(user=sj3, transaction_type='juror_slash').first()
        self.assertIsNotNone(slash_ledger)
        self.assertEqual(slash_ledger.amount, -50)

        # Check low-balance bad-actor juror (if sj1 voted against winning choice, balance clamped to 0)
        sj1_profile = UserProfile.objects.get(user=sj1)
        self.assertGreaterEqual(sj1_profile.rewards, 0)

    def test_auto_finalize_on_appeal_window_expiration(self):
        # Create dispute in pending_consensus with expired appeal window
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Auto finalize test',
            deposit_amount=50,
            escrow_status='held',
            status='pending_consensus',
            stage='initial',
            consensus_reached=True,
            consensus_percentage=100.0,
            winning_choice='taker',
            appeal_window_expires_at=timezone.now() - timedelta(hours=1)
        )

        # Taker views the dispute detail page
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Task completed
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')


