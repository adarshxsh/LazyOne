from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryPanel, JurorVote, DisputeAppeal, Friendship


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


class TwoTierDisputeAppealAndSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Friend of poster
        self.friend_of_poster = User.objects.create_user(username='friend_poster', password='password123')
        self.friend_poster_profile = UserProfile.objects.create(user=self.friend_of_poster, rewards=500)
        Friendship.objects.create(from_user=self.poster_profile, to_user=self.friend_poster_profile)

        # 12 Potential Jurors
        self.jurors = []
        for i in range(1, 13):
            user = User.objects.create_user(username=f'juror{i}', password='password123')
            UserProfile.objects.create(user=user, rewards=100)
            self.jurors.append(user)

        self.task = Task.objects.create(
            title="Disputed Delivery Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Conversation.objects.create(task=self.task)

    def test_juror_eligibility_exclusion(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )
        dispute = Dispute.objects.get(task=self.task)
        tier1_panel = dispute.jury_panels.get(tier=1)

        assigned_jurors = tier1_panel.jurors.all()
        self.assertEqual(assigned_jurors.count(), 3)
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.friend_of_poster, assigned_jurors)

    def test_tier1_voting_and_supermajority(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )
        dispute = Dispute.objects.get(task=self.task)
        panel = dispute.jury_panels.get(tier=1)
        jurors = list(panel.jurors.all())

        # Juror 1 & Juror 2 vote for taker (2/3 = 66.67% supermajority)
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'justification': 'Worker executed task properly'}
        )

        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.taker.id, 'justification': 'Agreed'}
        )

        # Juror 3 votes for poster (dissenting vote)
        self.client.login(username=jurors[2].username, password='password123')
        self.client.post(
            reverse('cast_juror_vote', args=[dispute.id]),
            {'voted_for': self.poster.id, 'justification': 'Disagreed'}
        )

        panel.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(panel.status, 'resolved')
        self.assertEqual(dispute.status, 'peer_review')

        # Check rewards and slashing for jurors
        jurors[0].userprofile.refresh_from_db()
        jurors[1].userprofile.refresh_from_db()
        jurors[2].userprofile.refresh_from_db()

        self.assertEqual(jurors[0].userprofile.rewards, 120)
        self.assertEqual(jurors[1].userprofile.rewards, 120)
        self.assertEqual(jurors[2].userprofile.rewards, 80)

        self.assertTrue(RewardLedger.objects.filter(user=jurors[0], transaction_type='juror_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=jurors[2], transaction_type='juror_slashing').exists())

    def test_appeal_window_and_tier2_escalation(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )
        dispute = Dispute.objects.get(task=self.task)
        panel = dispute.jury_panels.get(tier=1)
        jurors = list(panel.jurors.all())

        for j in jurors:
            self.client.login(username=j.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        dispute.refresh_from_db()
        self.assertTrue(dispute.can_be_appealed)

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('file_dispute_appeal', args=[dispute.id]),
            {'justification': 'Tier-1 decision was unfair'}
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'grand_jury_review')
        self.assertTrue(dispute.appeals.exists())

        appeal = dispute.appeals.first()
        self.assertEqual(appeal.appellant, self.poster)
        self.assertEqual(appeal.deposit_amount, self.task.deposit_bond_amount)

        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_deposit').exists())

        tier2_panel = dispute.jury_panels.get(tier=2)
        self.assertEqual(tier2_panel.quorum_size, 7)
        self.assertEqual(tier2_panel.jurors.count(), 7)

    def test_expired_appeal_window_rejection(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )
        dispute = Dispute.objects.get(task=self.task)
        panel = dispute.jury_panels.get(tier=1)
        jurors = list(panel.jurors.all())

        for j in jurors:
            self.client.login(username=j.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        panel.refresh_from_db()
        panel.resolved_at = timezone.now() - timedelta(hours=49)
        panel.save()

        dispute.refresh_from_db()
        self.assertFalse(dispute.can_be_appealed)

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('file_dispute_appeal', args=[dispute.id]),
            {'justification': 'Too late appeal'}
        )
        dispute.refresh_from_db()
        self.assertNotEqual(dispute.status, 'grand_jury_review')

    def test_tier2_grand_jury_resolution_and_slashing(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete instructions'}
        )
        dispute = Dispute.objects.get(task=self.task)
        tier1_panel = dispute.jury_panels.get(tier=1)
        for j in tier1_panel.jurors.all():
            self.client.login(username=j.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        self.client.login(username='poster', password='password123')
        self.client.post(
            reverse('file_dispute_appeal', args=[dispute.id]),
            {'justification': 'Tier-1 decision was erroneous'}
        )

        tier2_panel = dispute.jury_panels.get(tier=2)
        tier2_jurors = list(tier2_panel.jurors.all())

        for j in tier2_jurors[:5]:
            self.client.login(username=j.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.poster.id}
            )

        for j in tier2_jurors[5:]:
            self.client.login(username=j.username, password='password123')
            self.client.post(
                reverse('cast_juror_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'slashed')
        self.assertEqual(self.task.status, 'cancelled')

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='litigant_slashing').exists())

        honest_juror = tier2_jurors[0]
        dissenting_juror = tier2_jurors[5]

        self.assertTrue(RewardLedger.objects.filter(user=honest_juror, transaction_type='juror_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=dissenting_juror, transaction_type='juror_slashing').exists())


