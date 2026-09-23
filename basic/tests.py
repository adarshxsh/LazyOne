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


from basic.models import JurorAssignment, DisputeAppeal
from basic.services.dispute import DisputeService
from django.core.management import call_command

class MultiTierDisputeAppealTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Candidate jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=100)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

        self.juror4 = User.objects.create_user(username='juror4', password='password123')
        self.juror4_profile = UserProfile.objects.create(user=self.juror4, rewards=100)

        self.juror5 = User.objects.create_user(username='juror5', password='password123')
        self.juror5_profile = UserProfile.objects.create(user=self.juror5, rewards=100)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Appeals Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair requirements",
            deposit_amount=60,
            escrow_status='held',
            status='open'
        )

    def test_primary_dispute_66_percent_supermajority_quorum(self):
        # Assign 3 tier-1 jurors
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror1, tier=1)
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror2, tier=1)
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror3, tier=1)

        # Juror 1 & Juror 2 vote 'poster' (2 out of 3 = 66.7% supermajority)
        resolved, outcome = DisputeService.submit_juror_vote(self.dispute, self.juror1, 'poster', tier=1)
        self.assertFalse(resolved)

        resolved, outcome = DisputeService.submit_juror_vote(self.dispute, self.juror2, 'poster', tier=1)
        self.assertTrue(resolved)
        self.assertEqual(outcome, 'poster')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.primary_verdict, 'poster')
        self.assertEqual(self.dispute.status, 'tier1_resolved')
        self.assertIsNotNone(self.dispute.verdict_published_at)

        # Ensure task escrow funds are locked and task is still in disputed status
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_file_appeal_within_48_hours(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        initial_taker_rewards = self.taker_profile.rewards
        appeal = DisputeService.file_appeal(self.dispute, self.taker, "Disagree with tier 1 ruling")

        self.assertIsNotNone(appeal)
        self.assertEqual(appeal.status, 'pending')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards - 60)

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertEqual(JurorAssignment.objects.filter(dispute=self.dispute, tier=2).count(), 3)

    def test_active_appeal_pauses_expiry_resolution(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        DisputeService.file_appeal(self.dispute, self.taker, "Appealing verdict")

        call_command('resolve_expired_disputes')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_secondary_appeal_upheld_frivolous_loss_slashing(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror1, tier=1, vote='taker', voted_at=timezone.now())

        DisputeService.file_appeal(self.dispute, self.taker, "Appealing verdict")

        tier2_assignments = list(JurorAssignment.objects.filter(dispute=self.dispute, tier=2))
        self.assertEqual(len(tier2_assignments), 3)

        DisputeService.submit_juror_vote(self.dispute, tier2_assignments[0].juror, 'poster', tier=2)
        DisputeService.submit_juror_vote(self.dispute, tier2_assignments[1].juror, 'poster', tier=2)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.final_verdict, 'poster')

        appeal = self.dispute.appeal
        self.assertEqual(appeal.status, 'upheld')

        litigant_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='litigant_slashing').first()
        self.assertIsNotNone(litigant_ledger)

        juror1_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_slashing').first()
        self.assertIsNotNone(juror1_ledger)
        self.assertEqual(juror1_ledger.amount, -20)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 80)

    def test_secondary_appeal_reversed_appellant_wins_stake_refunded(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror1, tier=1, vote='poster', voted_at=timezone.now())

        DisputeService.file_appeal(self.dispute, self.taker, "Appealing verdict")

        tier2_assignments = list(JurorAssignment.objects.filter(dispute=self.dispute, tier=2))

        DisputeService.submit_juror_vote(self.dispute, tier2_assignments[0].juror, 'taker', tier=2)
        DisputeService.submit_juror_vote(self.dispute, tier2_assignments[1].juror, 'taker', tier=2)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.final_verdict, 'taker')

        appeal = self.dispute.appeal
        self.assertEqual(appeal.status, 'reversed')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500 + 300 + 60)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_refund').first()
        self.assertIsNotNone(refund_ledger)

        juror1_ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_slashing').first()
        self.assertIsNotNone(juror1_ledger)

    def test_juror_slashing_zero_balance_guard(self):
        self.juror1_profile.rewards = 10
        self.juror1_profile.save()

        self.dispute.primary_verdict = 'poster'
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror1, tier=1, vote='taker', voted_at=timezone.now())

        DisputeService.finalize_uncontested_dispute(self.dispute)

        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 0)

        ledger = RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_slashing').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -10)

    def test_secondary_panel_guardrails(self):
        JurorAssignment.objects.create(dispute=self.dispute, juror=self.juror1, tier=1)

        panel = DisputeService.assemble_juror_panel(self.dispute, tier=2, count=3)
        assigned_jurors = [assignment.juror for assignment in panel]

        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.juror1, assigned_jurors)

    def test_uncontested_dispute_finalized_after_48_hours(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now() - timedelta(hours=50) # 50 hours ago (> 48h)
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        # Run management command
        call_command('resolve_expired_disputes')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.final_verdict, 'poster')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

    def test_unauthorized_user_cannot_appeal(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        # Juror1 (not a task litigant) attempts to appeal
        self.assertFalse(self.dispute.can_appeal(self.juror1))
        with self.assertRaises(ValueError):
            DisputeService.file_appeal(self.dispute, self.juror1, "Unauthorized appeal attempt")

    def test_insufficient_balance_cannot_appeal(self):
        self.dispute.primary_verdict = 'poster'
        self.dispute.verdict_published_at = timezone.now()
        self.dispute.status = 'tier1_resolved'
        self.dispute.save()

        self.taker_profile.rewards = 10 # Deposit bond is 60 (> 10)
        self.taker_profile.save()

        self.assertTrue(self.dispute.can_appeal(self.taker))
        with self.assertRaises(ValueError):
            DisputeService.file_appeal(self.dispute, self.taker, "Low balance appeal")


