from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeVote, DisputeAppeal, RewardLedger

class DisputeAppealConsensusSlashingTests(TestCase):
    def setUp(self):
        # Create poster, doer, jurors, and staff user
        self.poster = User.objects.create_user(username='poster', password='password')
        self.doer = User.objects.create_user(username='doer', password='password')
        self.juror1 = User.objects.create_user(username='juror1', password='password')
        self.juror2 = User.objects.create_user(username='juror2', password='password')
        self.juror3 = User.objects.create_user(username='juror3', password='password')
        self.staff_user = User.objects.create_user(username='staff', password='password', is_staff=True)

        # Profiles created by signals or manually ensure reward balances
        for u in [self.poster, self.doer, self.juror1, self.juror2, self.juror3, self.staff_user]:
            profile, _ = UserProfile.objects.get_or_create(user=u)
            profile.rewards = 1000
            profile.save()

        # Create task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.doer,
            status='in_progress'
        )

        # Raise dispute
        self.client = Client()
        self.client.login(username='doer', password='password')
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason='Incomplete deliverable disputed'
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_dispute_status_choices(self):
        """Verify dispute state machine choices include open, appealed, under_review, resolved."""
        statuses = [choice[0] for choice in Dispute.STATUS_CHOICES]
        self.assertIn('open', statuses)
        self.assertIn('appealed', statuses)
        self.assertIn('under_review', statuses)
        self.assertIn('resolved', statuses)

    def test_quorum_and_supermajority_consensus_primary_verdict(self):
        """Verify 66% supermajority and minimum quorum (3 votes) are required for primary verdict."""
        # Vote 1 (Poster) -> Total 1 (Quorum not met)
        self.client.login(username='juror1', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.poster.id})
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.primary_winner)

        # Vote 2 (Poster) -> Total 2 (Quorum not met)
        self.client.login(username='juror2', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.poster.id})
        self.dispute.refresh_from_db()
        self.assertIsNone(self.dispute.primary_winner)

        # Vote 3 (Poster) -> Total 3 (Quorum met, 100% >= 66%)
        self.client.login(username='juror3', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.poster.id})
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.primary_winner, self.poster)
        self.assertIsNotNone(self.dispute.primary_verdict_at)
        self.assertTrue(self.dispute.is_appeal_window_active())

    def test_file_appeal_bond_escrow_deduction(self):
        """Verify filing an appeal deducts bond points and records appeal_bond_escrow in RewardLedger."""
        # Establish primary verdict for poster
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror2, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror3, voted_for=self.poster, tier=1)
        self.dispute.primary_winner = self.poster
        self.dispute.primary_verdict_at = timezone.now()
        self.dispute.save()

        # Doer files appeal with 100 bond points
        self.client.login(username='doer', password='password')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'bond_amount': 100,
            'reason': 'Poster provided incorrect requirements'
        })
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertTrue(hasattr(self.dispute, 'appeal'))
        self.assertEqual(self.dispute.appeal.bond_amount, 100)

        # Doer profile updated
        self.doer.userprofile.refresh_from_db()
        self.assertEqual(self.doer.userprofile.rewards, 900)

        # Ledger transaction created
        tx = RewardLedger.objects.filter(user=self.doer, transaction_type='appeal_bond_escrow').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, -100)

    def test_reversing_verdict_triggers_bond_refund_and_juror_slashing(self):
        """Reversing a verdict on appeal triggers bond refund to appellant and slashing of primary dissenting jurors."""
        # Primary verdict in favor of poster: juror1 and juror2 voted poster, juror3 voted doer (dissenting juror)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror2, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror3, voted_for=self.poster, tier=1)
        self.dispute.primary_winner = self.poster
        self.dispute.primary_verdict_at = timezone.now()
        self.dispute.save()

        # Doer appeals
        self.client.login(username='doer', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/appeal/', {'bond_amount': 100, 'reason': 'Appeal claim'})

        # Tier 2 Appeal Council votes in favor of doer
        juror4 = User.objects.create_user(username='juror4', password='password')
        juror5 = User.objects.create_user(username='juror5', password='password')
        juror6 = User.objects.create_user(username='juror6', password='password')
        for j in [juror4, juror5, juror6]:
            p, _ = UserProfile.objects.get_or_create(user=j)
            p.rewards = 1000
            p.save()

        self.client.login(username='juror4', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.doer.id})
        self.client.login(username='juror5', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.doer.id})
        self.client.login(username='juror6', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.doer.id})

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.final_winner, self.doer)

        # Doer bond refunded (900 + 100 = 1000) + task reward (200) = 1200
        self.doer.userprofile.refresh_from_db()
        self.assertEqual(self.doer.userprofile.rewards, 1200)

        # Check bond refund ledger
        refund_tx = RewardLedger.objects.filter(user=self.doer, transaction_type='appeal_bond_refund').first()
        self.assertIsNotNone(refund_tx)
        self.assertEqual(refund_tx.amount, 100)

        # Primary dissenting jurors (who voted for poster) slashed 50 points
        for juror in [self.juror1, self.juror2, self.juror3]:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 950)
            slashed_tx = RewardLedger.objects.filter(user=juror, transaction_type='juror_slashing').first()
            self.assertIsNotNone(slashed_tx)
            self.assertEqual(slashed_tx.amount, -50)

    def test_slashing_floor_guardrail_prevents_negative_balance(self):
        """Ensure slashing penalties cannot reduce a user reward balance below zero."""
        # Set juror1 rewards to 30 (less than 50 slashing penalty)
        self.juror1.userprofile.rewards = 30
        self.juror1.userprofile.save()

        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror2, voted_for=self.poster, tier=1)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror3, voted_for=self.poster, tier=1)
        self.dispute.primary_winner = self.poster
        self.dispute.primary_verdict_at = timezone.now()
        self.dispute.save()

        # Doer appeals
        self.client.login(username='doer', password='password')
        self.client.post(f'/dispute/{self.dispute.id}/appeal/', {'bond_amount': 100, 'reason': 'Appeal'})

        # Appeal council votes doer
        j4 = User.objects.create_user(username='j4', password='password')
        j5 = User.objects.create_user(username='j5', password='password')
        j6 = User.objects.create_user(username='j6', password='password')
        for j in [j4, j5, j6]:
            UserProfile.objects.create(user=j, rewards=1000)

        for j in [j4, j5, j6]:
            self.client.login(username=j.username, password='password')
            self.client.post(f'/dispute/{self.dispute.id}/vote/', {'voted_for_id': self.doer.id})

        self.juror1.userprofile.refresh_from_db()
        self.assertEqual(self.juror1.userprofile.rewards, 0)  # Cannot go below 0

    def test_staff_override_authorization(self):
        """Staff administrators can override and finalize disputes when quorums are not met."""
        self.client.login(username='staff', password='password')
        response = self.client.post(f'/dispute/{self.dispute.id}/staff-resolve/', {
            'winner_id': self.doer.id
        })
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.final_winner, self.doer)
