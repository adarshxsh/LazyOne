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


class TwoTierJuryGovernanceTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Poster & Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create 5 Neutral Jurors
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=500)
            self.jurors.append(juror)

        # Create Task (Reward = 300)
        self.task = Task.objects.create(
            title="Governance Task",
            description="Task to test dispute escalation",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        Conversation.objects.create(task=self.task)

    def test_primary_voting_quorum_and_consensus(self):
        # Taker raises dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair rejection'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 votes 'poster'
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open') # Only 1 vote, quorum (3) not met

        # Juror 2 votes 'taker'
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'taker'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open') # 2 votes, quorum (3) not met

        # Juror 3 votes 'poster' (2 out of 3 = 66.67% >= 66% supermajority)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'primary_resolved')
        self.assertEqual(dispute.primary_outcome, 'poster')
        self.assertTrue(dispute.is_in_appeal_window)

    def test_litigant_cannot_vote_as_juror(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})
        self.assertEqual(dispute.votes.count(), 0)

    def test_appeal_submission_requires_100_percent_reward_bond(self):
        # Raise dispute and trigger primary resolution favoring poster
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        for i in [1, 2, 3]:
            self.client.login(username=f'juror{i}', password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'primary_resolved')

        # Taker profile balance initially: 500 - 60 (deposit) = 440
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 440)

        # Taker submits appeal (task reward = 300)
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('appeal_dispute', args=[dispute.id]), {'reason': 'Evidence attached'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'appealed')

        # Balance debited 300 -> 140 left
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 140)

        # Check appeal bond held in ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_bond_held').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -300)

    def test_appeal_upheld_slashes_bad_faith_primary_juror(self):
        # Setup dispute with primary outcome = 'poster' (Juror 1 & 2 voted 'poster', Juror 3 voted 'taker')
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'taker'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.primary_outcome, 'poster')

        # Taker appeals
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('appeal_dispute', args=[dispute.id]), {'reason': 'Valid grounds'})

        # Senior panel (Juror 3, 4, 5) votes 'taker' -> Overturns primary decision!
        for i in [3, 4, 5]:
            self.client.login(username=f'juror{i}', password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'senior', 'vote': 'taker'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.final_winner, 'taker')

        # Primary Jurors 1 & 2 voted 'poster' against final consensus 'taker' -> Slashed 100 points!
        j1_profile = self.jurors[0].userprofile
        j1_profile.refresh_from_db()
        self.assertEqual(j1_profile.rewards, 400) # 500 - 100 = 400

        # Check juror slash ledger
        slash_ledger = RewardLedger.objects.filter(user=self.jurors[0], transaction_type='juror_slash').first()
        self.assertIsNotNone(slash_ledger)
        self.assertEqual(slash_ledger.amount, -100)

    def test_appeal_rejected_forfeits_bond_to_winner(self):
        # Primary outcome = 'poster'
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        for i in [1, 2, 3]:
            self.client.login(username=f'juror{i}', password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})

        # Taker appeals (300 bond held)
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('appeal_dispute', args=[dispute.id]), {'reason': 'Frivolous appeal'})

        # Senior panel votes 'poster' -> Appeal Rejected!
        for i in [3, 4, 5]:
            self.client.login(username=f'juror{i}', password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'senior', 'vote': 'poster'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.final_winner, 'poster')

        # Poster receives 80% of forfeited 300 bond (240 points) + task cancellation refund
        poster_forfeit_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_bond_forfeit').first()
        self.assertIsNotNone(poster_forfeit_ledger)
        self.assertEqual(poster_forfeit_ledger.amount, 240)

    def test_resolve_expired_disputes_command_applies_penalties(self):
        from django.core.management import call_command

        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Expired dispute'})
        dispute = Dispute.objects.get(task=self.task)

        # Juror 1 voted, Jurors 2-5 did not vote
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'tier': 'primary', 'vote': 'poster'})

        # Backdate dispute created_at by 3 days
        dispute.created_at = timezone.now() - timedelta(days=3)
        dispute.save()

        # Run resolution job
        call_command('resolve_expired_disputes', days=2)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Non-voting assigned juror (Juror 2) gets 30-day juror ineligibility penalty
        j2_profile = self.jurors[1].userprofile
        j2_profile.refresh_from_db()
        self.assertIsNotNone(j2_profile.juror_ineligible_until)
        self.assertFalse(j2_profile.is_juror_eligible)


