from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.exceptions import ValidationError

from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment, DisputeAuditEvent, Friendship
from .services.dispute import DisputeService
from .jury import select_jurors_for_dispute


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
        self.assertEqual(self.task.deposit_bond_amount, 60)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
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

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertIn(dispute.status, ['open', 'voting'])
        self.assertEqual(dispute.raised_by, self.taker)

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'withdrawn')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

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

        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


class JurorSelectionAndAntiBiasTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster1', password='pass')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker1', password='pass')
        UserProfile.objects.create(user=self.taker, rewards=1000)

        self.friend = User.objects.create_user(username='friend1', password='pass')
        friend_prof = UserProfile.objects.create(user=self.friend, rewards=1000)
        self.poster.userprofile.friends.add(friend_prof)

        # Create candidate jurors
        self.jurors = []
        for i in range(10):
            u = User.objects.create_user(username=f'juror_{i}', password='pass')
            UserProfile.objects.create(user=u, rewards=500)
            self.jurors.append(u)

        self.task = Task.objects.create(
            title="Bias Test Task", description="Desc", reward=200,
            posted_by=self.poster, taken_by=self.taker, status='in_progress'
        )
        self.dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Testing bias",
            deposit_amount=50, escrow_status='held'
        )

    def test_juror_selection_excludes_participants_and_friends(self):
        assignments = select_jurors_for_dispute(self.dispute, panel_size=3, tier=1, stake_amount=50)
        selected_users = [a.juror for a in assignments]

        self.assertEqual(len(selected_users), 3)
        self.assertNotIn(self.poster, selected_users)
        self.assertNotIn(self.taker, selected_users)
        self.assertNotIn(self.friend, selected_users)

        for a in assignments:
            self.assertEqual(a.tier, 1)
            self.assertEqual(a.stake_amount, 50)
            a.juror.userprofile.refresh_from_db()
            self.assertEqual(a.juror.userprofile.rewards, 450) # 500 - 50 = 450

    def test_tier2_selection_excludes_tier1_jurors(self):
        t1_assignments = select_jurors_for_dispute(self.dispute, panel_size=3, tier=1, stake_amount=50)
        t1_jurors = [a.juror for a in t1_assignments]

        t2_assignments = select_jurors_for_dispute(self.dispute, panel_size=3, tier=2, stake_amount=100)
        t2_jurors = [a.juror for a in t2_assignments]

        self.assertEqual(len(t2_jurors), 3)
        for j in t2_jurors:
            self.assertNotIn(j, t1_jurors)
            self.assertNotIn(j, [self.poster, self.taker, self.friend])


class AppealEscalationAndJurorSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster_app', password='pass')
        self.poster_prof = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_app', password='pass')
        self.taker_prof = UserProfile.objects.create(user=self.taker, rewards=1000)

        # 6 jurors: 3 for tier 1, 3 for tier 2
        self.jurors = []
        for i in range(6):
            u = User.objects.create_user(username=f'juror_app_{i}', password='pass')
            UserProfile.objects.create(user=u, rewards=1000, reputation_score=100)
            self.jurors.append(u)

        self.task = Task.objects.create(
            title="Appeal Task", description="Appeal Desc", reward=200,
            posted_by=self.poster, taken_by=self.taker, status='in_progress'
        )

    def test_full_appeal_overturn_and_dishonest_juror_slashing(self):
        # 1. Taker raises dispute
        dispute = DisputeService.raise_dispute(self.task, self.taker, "Incomplete work claim")
        self.assertEqual(dispute.status, 'voting')
        self.assertEqual(dispute.tier, 1)

        t1_assignments = list(dispute.juror_assignments.filter(tier=1))
        self.assertEqual(len(t1_assignments), 3)

        # 2. Tier 1 Jurors vote: 2 vote 'poster', 1 votes 'taker' -> Poster wins Tier 1
        j0, j1, j2 = t1_assignments[0].juror, t1_assignments[1].juror, t1_assignments[2].juror
        DisputeService.cast_juror_vote(dispute, j0, 'poster')
        DisputeService.cast_juror_vote(dispute, j1, 'poster')
        DisputeService.cast_juror_vote(dispute, j2, 'taker')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winner, self.poster)
        self.assertEqual(dispute.consensus_outcome, 'poster')

        # 3. Taker files Appeal (Tier 2 Escalation)
        self.assertTrue(dispute.is_appealable())
        dispute = DisputeService.file_appeal(dispute, self.taker, "Tier 1 decision was biased")

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'appealed')
        self.assertEqual(dispute.tier, 2)
        self.assertEqual(dispute.appealed_by, self.taker)
        self.assertEqual(dispute.appeal_bond_amount, 100) # max(100, 50*2)

        t2_assignments = list(dispute.juror_assignments.filter(tier=2))
        self.assertEqual(len(t2_assignments), 3)

        # Confirm no overlap between Tier 1 and Tier 2 jurors
        t1_ids = {a.juror_id for a in t1_assignments}
        t2_ids = {a.juror_id for a in t2_assignments}
        self.assertTrue(t1_ids.isdisjoint(t2_ids))

        # 4. Tier 2 Jurors vote: all 3 vote 'taker' -> OVERTURNS decision!
        for a in t2_assignments:
            DisputeService.cast_juror_vote(dispute, a.juror, 'taker')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'appeal_reversed')
        self.assertEqual(dispute.winner, self.taker)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check that Taker's appeal bond was refunded
        self.assertEqual(dispute.appeal_escrow_status, 'refunded')
        self.taker_prof.refresh_from_db()
        self.assertGreaterEqual(self.taker_prof.rewards, 1000)

        # CHECK DISHONEST TIER 1 JUROR SLASHING
        # j0 and j1 voted 'poster' in Tier 1 (which was overturned) -> dishonest/colluding!
        j0.userprofile.refresh_from_db()
        j1.userprofile.refresh_from_db()
        j2.userprofile.refresh_from_db()

        self.assertEqual(j0.userprofile.reputation_score, 85) # 100 - 15 = 85
        self.assertEqual(j1.userprofile.reputation_score, 85)

        # Check ledger for dishonest slashing entries
        j0_slash = RewardLedger.objects.filter(user=j0, transaction_type='juror_slashing').first()
        self.assertIsNotNone(j0_slash)

    def test_appeal_insufficient_rewards_rejected(self):
        dispute = DisputeService.raise_dispute(self.task, self.taker, "Reason")
        t1_assignments = list(dispute.juror_assignments.filter(tier=1))
        for a in t1_assignments:
            DisputeService.cast_juror_vote(dispute, a.juror, 'poster')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Reduce taker rewards below appeal bond (100 required)
        self.taker_prof.rewards = 20
        self.taker_prof.save()

        with self.assertRaises(ValidationError):
            DisputeService.file_appeal(dispute, self.taker, "Appeal attempt")

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')


class DisputeAuditEventTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster_audit', password='pass')
        UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker = User.objects.create_user(username='taker_audit', password='pass')
        UserProfile.objects.create(user=self.taker, rewards=1000)

        self.task = Task.objects.create(
            title="Audit Task", description="Desc", reward=100,
            posted_by=self.poster, taken_by=self.taker, status='in_progress'
        )

    def test_audit_event_immutability(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Audit test", deposit_amount=50
        )
        event = DisputeAuditEvent.objects.create(
            dispute=dispute, actor=self.taker, event_type='test_event', details_json={'key': 'value'}
        )

        # Updating event should raise ValueError
        event.event_type = 'modified_type'
        with self.assertRaises(ValueError):
            event.save()

        # Deleting event should raise ValueError
        with self.assertRaises(ValueError):
            event.delete()
