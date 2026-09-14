from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeVote, RewardLedger

class MultiTierDisputeEngineTests(TestCase):
    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        self.taker_profile.rewards = 1000
        self.taker_profile.save()

        # Create staff user for Appellate Council
        self.admin = User.objects.create_superuser(username='admin', password='password123')

        # Create 4 potential jurors
        self.jurors = []
        for i in range(1, 5):
            juror = User.objects.create_user(username=f'juror{i}', password='password123')
            juror_profile, _ = UserProfile.objects.get_or_create(user=juror, defaults={'rewards': 500})
            juror_profile.rewards = 500
            juror_profile.save()
            # Give each juror at least 3 completed tasks to be eligible
            for j in range(3):
                Task.objects.create(
                    title=f'Completed task {j} by juror{i}',
                    description='Desc',
                    reward=50,
                    posted_by=juror,
                    status='completed'
                )
            self.jurors.append(juror)

        # Create a task and dispute
        self.task = Task.objects.create(
            title='Contested Web Design',
            description='Design a homepage',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_dispute_creation_initial_state(self):
        """Dispute creation sets state to open"""
        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/task/dispute/{self.task.id}/', {'reason': 'Work delivered but unpaid.'})
        self.assertEqual(response.status_code, 302)
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.task.status, 'disputed')

    def test_juror_eligibility_guardrails(self):
        """Task poster and taker cannot vote, and jurors must have >= 3 completed tasks"""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason', status='open')

        # Poster cannot vote
        can_vote_poster, reason_poster = dispute.can_vote(self.poster)
        self.assertFalse(can_vote_poster)
        self.assertIn("cannot participate as jurors", reason_poster)

        # Taker cannot vote
        can_vote_taker, reason_taker = dispute.can_vote(self.taker)
        self.assertFalse(can_vote_taker)
        self.assertIn("cannot participate as jurors", reason_taker)

        # Ineligible user with < 3 completed tasks
        noob = User.objects.create_user(username='noob', password='password123')
        UserProfile.objects.create(user=noob, rewards=500)
        can_vote_noob, reason_noob = dispute.can_vote(noob)
        self.assertFalse(can_vote_noob)
        self.assertIn("completed at least 3 tasks", reason_noob)

        # Eligible juror
        can_vote_juror, _ = dispute.can_vote(self.jurors[0])
        self.assertTrue(can_vote_juror)

    def test_peer_jury_quorum_and_supermajority_consensus(self):
        """Test voting quorum of 3 and 66% supermajority consensus triggering Tier 1 resolution"""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Reason', status='open')

        # Vote 1 (Juror 1 votes for taker)
        self.client.login(username='juror1', password='password123')
        self.client.post(f'/dispute/vote/{dispute.id}/', {'vote': 'taker'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting') # Transitioned to voting
        self.assertEqual(dispute.votes.count(), 1)

        # Vote 2 (Juror 2 votes for poster)
        self.client.login(username='juror2', password='password123')
        self.client.post(f'/dispute/vote/{dispute.id}/', {'vote': 'poster'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'voting') # Quorum not met yet (2 < 3)

        # Vote 3 (Juror 3 votes for taker) -> Total: 2 taker, 1 poster (2/3 = 66.67% >= 66%) -> Tier 1 resolved!
        self.client.login(username='juror3', password='password123')
        self.client.post(f'/dispute/vote/{dispute.id}/', {'vote': 'taker'})
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved_tier1')
        self.assertEqual(dispute.winner, self.taker)
        self.assertEqual(dispute.winning_vote, 'taker')

        # Check taker reward payout
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1100) # 1000 + 100

        # Check mediation reward for consensus jurors (juror1 & juror3)
        j1_profile = UserProfile.objects.get(user=self.jurors[0])
        j3_profile = UserProfile.objects.get(user=self.jurors[2])
        self.assertEqual(j1_profile.rewards, 510) # 500 + 10
        self.assertEqual(j3_profile.rewards, 510) # 500 + 10

        # Check juror slashing for minority juror (juror2)
        j2_profile = UserProfile.objects.get(user=self.jurors[1])
        self.assertEqual(j2_profile.rewards, 490) # 500 - 10

        # Check RewardLedger records
        self.assertTrue(RewardLedger.objects.filter(user=self.jurors[0], transaction_type='mediation_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.jurors[1], transaction_type='juror_slashing').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='litigant_slashing').exists())

    def test_appellate_escalation_within_48_hours(self):
        """Losing party can appeal Tier 1 ruling within 48h upon filing appeal bond"""
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Reason',
            status='resolved_tier1', winner=self.taker, winning_vote='taker',
            resolved_tier1_at=timezone.now()
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/appeal/{dispute.id}/')
        self.assertEqual(response.status_code, 302)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'appealed')
        self.assertEqual(dispute.appellant, self.poster)
        self.assertEqual(dispute.appeal_bond_amount, 100)

        # Check poster rewards deducted for appeal bond
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 900) # 1000 - 100

        # Check RewardLedger for appeal bond slash/deposit
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_bond_slash').exists())

    def test_appeal_window_expiration(self):
        """Appeal fails after 48-hour window has passed"""
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Reason',
            status='resolved_tier1', winner=self.taker, winning_vote='taker',
            resolved_tier1_at=timezone.now() - timedelta(hours=49) # 49 hours ago
        )

        can_appeal, reason = dispute.can_appeal(self.poster)
        self.assertFalse(can_appeal)
        self.assertIn("expired", reason)

        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/appeal/{dispute.id}/')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved_tier1') # Unchanged

    def test_appellate_council_uphold_appeal(self):
        """Appellate council upholding decision forfeits appellant's bond"""
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Reason',
            status='appealed', winner=self.taker, winning_vote='taker',
            appellant=self.poster, appeal_bond_amount=100,
            resolved_tier1_at=timezone.now(), appealed_at=timezone.now()
        )

        self.client.login(username='admin', password='password123')
        response = self.client.post(f'/dispute/resolve-appeal/{dispute.id}/', {'decision': 'uphold'})
        self.assertEqual(response.status_code, 302)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved_tier2')
        self.assertEqual(dispute.tier2_decision, 'uphold')
        self.assertEqual(dispute.winner, self.taker)

        # Frivolous appellant poster lost posted bond
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='litigant_slashing').exists())

    def test_appellate_council_overturn_appeal(self):
        """Appellate council overturning decision refunds bond and transfers reward to appellant"""
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason='Reason',
            status='appealed', winner=self.taker, winning_vote='taker',
            appellant=self.poster, appeal_bond_amount=100,
            resolved_tier1_at=timezone.now(), appealed_at=timezone.now()
        )
        # Taker received initial 100 reward points from Tier 1
        self.taker_profile.rewards = 1100
        self.taker_profile.save()

        # Poster paid 100 appeal bond
        self.poster_profile.rewards = 900
        self.poster_profile.save()

        self.client.login(username='admin', password='password123')
        response = self.client.post(f'/dispute/resolve-appeal/{dispute.id}/', {'decision': 'overturn'})
        self.assertEqual(response.status_code, 302)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved_tier2')
        self.assertEqual(dispute.tier2_decision, 'overturn')
        self.assertEqual(dispute.winner, self.poster)

        # Poster refunded bond (100) + awarded task reward (100) = 900 + 200 = 1100
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1100)

        # Taker penalized task reward (100) = 1100 - 100 = 1000
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1000)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='litigant_slashing').exists())
