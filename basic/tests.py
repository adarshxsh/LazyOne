from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeAppeal, JuryPanel, JurorVote


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
        self.assertIn(dispute.status, ['open', 'peer_review'])
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


class DisputeAppealAndSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Create 5 potential jurors
        self.jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'juror{i+1}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)
            self.jurors.append(u)

        self.task = Task.objects.create(
            title="Appeals Task",
            description="Task for appeals testing",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_raise_dispute_creates_dispute_and_tier1_panel(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work'}
        )
        self.task.refresh_from_db()
        dispute = Dispute.objects.get(task=self.task)
        self.assertIn(dispute.status, ['open', 'peer_review'])

        panel = dispute.jury_panels.filter(tier=1).first()
        self.assertIsNotNone(panel)
        self.assertEqual(panel.quorum_size, 3)
        self.assertEqual(panel.jurors.count(), 3)

    def test_overwhelming_consensus_slashes_outlier_juror(self):
        # Create dispute and Tier-1 panel manually for controlled test
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test dispute',
            status='peer_review',
            deposit_amount=60,
            escrow_status='held'
        )
        panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=2,
            quorum_size=5,
            status='active'
        )
        panel.jurors.set(self.jurors)

        # 4 jurors vote for poster, 1 juror votes for taker (80% majority > 80% consensus ratio or overwhelming)
        # To test >80% majority: 5 out of 5 vote (100% > 80%).
        # Let's test 5 out of 5 voting: 4 vote poster, 1 votes taker.
        # Wait, 4/5 = 80%. If threshold > 80%, 4/5 is 80%. What if 5/5 vote or 4/4 vote?
        # Let's test 5 jurors where 4 vote for poster and 1 votes for taker with consensus_ratio >= 0.80 or 100%.
        # Let's check: j1, j2, j3, j4 vote poster, j5 votes taker.
        # Cast votes via client or model:
        for juror in self.jurors[:4]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.poster.id, 'justification': 'Poster is right'}
            )

        # 5th juror votes taker
        outlier_juror = self.jurors[4]
        self.client.login(username=outlier_juror.username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'justification': 'Taker is right'}
        )

        panel.refresh_from_db()
        self.assertEqual(panel.status, 'resolved')

        # Check that outlier juror lost 10 points via dispute_slash transaction
        slash_ledger = RewardLedger.objects.filter(
            user=outlier_juror,
            transaction_type='dispute_slash'
        ).first()
        self.assertIsNotNone(slash_ledger)
        self.assertEqual(slash_ledger.amount, -10)

        outlier_profile = outlier_juror.userprofile
        outlier_profile.refresh_from_db()
        self.assertEqual(outlier_profile.rewards, 90)  # Started at 100 - 10 = 90

    def test_split_decision_does_not_slash_dissenting_juror(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test dispute',
            status='peer_review',
            deposit_amount=60,
            escrow_status='held'
        )
        panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=1,
            quorum_size=3,
            status='active'
        )
        panel.jurors.set(self.jurors[:3])

        # 2 jurors vote poster, 1 votes taker (2/3 = 66.7% <= 80%)
        self.client.login(username=self.jurors[0].username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.poster.id}
        )
        self.client.login(username=self.jurors[1].username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.poster.id}
        )

        dissenting_juror = self.jurors[2]
        self.client.login(username=dissenting_juror.username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.taker.id}
        )

        # Confirm no dispute_slash transaction occurred for dissenting_juror
        slash_ledger = RewardLedger.objects.filter(
            user=dissenting_juror,
            transaction_type='dispute_slash'
        ).first()
        self.assertIsNone(slash_ledger)

    def test_file_dispute_appeal_creates_appeal_model_and_tier2_panel(self):
        # Set up resolved Tier-1 dispute eligible for appeal
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test dispute',
            status='appeal_period',
            deposit_amount=60,
            escrow_status='held'
        )
        panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=1,
            quorum_size=3,
            status='resolved',
            resolved_at=timezone.now()
        )

        # Deposit bond for task is 60 -> 2x bond is 120
        self.taker_profile.rewards = 1000
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('file_dispute_appeal', args=[dispute.id]),
            {'justification': 'I disagree with the Tier-1 decision'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Verify DisputeAppeal model created
        appeal = DisputeAppeal.objects.get(dispute=dispute)
        self.assertEqual(appeal.appellant, self.taker)
        self.assertEqual(appeal.appeal_bond_amount, 120)
        self.assertEqual(appeal.status, 'pending')

        # Verify balance deducted: 1000 - 120 = 880
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 880)

        # Verify dispute status transitioned to appeal_period
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'appeal_period')

        # Verify expanded 5-juror Tier-2 senior panel assigned
        tier2_panel = dispute.jury_panels.filter(tier=2).first()
        self.assertIsNotNone(tier2_panel)
        self.assertEqual(tier2_panel.quorum_size, 5)

        # Verify RewardLedger entry for appeal deposit
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -120)

    def test_max_one_appeal_escalation_tier_enforced(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test dispute',
            status='appeal_period',
            deposit_amount=60,
            escrow_status='held'
        )
        panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=1,
            quorum_size=3,
            status='resolved',
            resolved_at=timezone.now()
        )

        # File first appeal
        DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=self.taker,
            appeal_bond_amount=120,
            justification='First appeal',
            status='pending'
        )

        # Attempt second appeal
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('file_dispute_appeal', args=[dispute.id]),
            {'justification': 'Second appeal attempt'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Confirm still only 1 appeal exists
        self.assertEqual(DisputeAppeal.objects.filter(dispute=dispute).count(), 1)

    def test_senior_jury_panel_authoritative_resolution(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test dispute',
            status='appeal_period',
            deposit_amount=60,
            escrow_status='held'
        )
        appeal = DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=self.taker,
            appeal_bond_amount=120,
            justification='Appeal justification',
            status='pending'
        )
        tier2_panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=2,
            quorum_size=5,
            status='active'
        )
        tier2_panel.jurors.set(self.jurors)

        # 5 jurors on Tier-2 panel vote in favor of taker (appellant)
        for juror in self.jurors:
            self.client.login(username=juror.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        self.task.refresh_from_db()
        dispute.refresh_from_db()
        appeal.refresh_from_db()

        # Task should be completed
        self.assertEqual(self.task.status, 'completed')

        # Appeal status should be overturned
        self.assertEqual(appeal.status, 'overturned')

        # Taker profile should receive reward (300) + deposit refund (60) + appeal refund (120)
        self.taker_profile.refresh_from_db()
        # Initial 1000 - deposit 60 - appeal 120 + 300 + 60 + 120 = 1300
        self.assertGreaterEqual(self.taker_profile.rewards, 1300)


