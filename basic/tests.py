from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Appeal, AppealJuror, Notification


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


class PeerJurorAppealAndSlashingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Litigants
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Peer Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=200)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=200)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=200)

        # Task and Resolved Dispute
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Appealed Task",
            description="Appealed Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='completed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unfair initial ruling",
            deposit_amount=60,
            escrow_status='held',
            status='resolved',
            resolved_at=timezone.now()
        )

    def test_file_appeal_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        # Deposit bond is 60. Taker balance was 500 -> 440
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 440)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'under_appeal')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'under_appeal')

        appeal = Appeal.objects.get(dispute=self.dispute)
        self.assertEqual(appeal.appellant, self.taker)
        self.assertEqual(appeal.appeal_deposit, 60)
        self.assertEqual(appeal.status, 'pending')

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

        # Check jurors assigned
        juror_assignments = AppealJuror.objects.filter(appeal=appeal)
        self.assertTrue(juror_assignments.exists())
        for assignment in juror_assignments:
            self.assertNotIn(assignment.juror, [self.poster, self.taker])

    def test_file_appeal_outside_sla_window(self):
        # Set resolved_at to 73 hours ago
        self.dispute.resolved_at = timezone.now() - timedelta(hours=73)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(Appeal.objects.filter(dispute=self.dispute).exists())
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_litigant_cannot_vote_as_juror(self):
        appeal = Appeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            appeal_deposit=60,
            status='pending'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('cast_juror_vote', args=[appeal.id]),
            {'vote': 'poster'}
        )

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(AppealJuror.objects.filter(appeal=appeal, juror=self.poster, vote='poster').exists())

    def test_juror_consensus_appellant_wins_and_slashing(self):
        # Taker balance: 500 initial - 60 dispute bond - 60 appeal bond = 380
        self.taker_profile.rewards = 380
        self.taker_profile.save()

        appeal = Appeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            appeal_deposit=60,
            status='pending',
            quorum=3,
            consensus_threshold=0.66
        )
        AppealJuror.objects.create(appeal=appeal, juror=self.juror1)
        AppealJuror.objects.create(appeal=appeal, juror=self.juror2)
        AppealJuror.objects.create(appeal=appeal, juror=self.juror3)

        # Juror 1 votes for Taker (Appellant)
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'taker'})

        # Juror 2 votes for Taker (Appellant)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'taker'})

        # Juror 3 votes for Poster (Appellee - minority dishonest)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'poster'})

        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'resolved')
        self.assertEqual(appeal.ruling, 'upheld')

        # Taker (Appellant) won: gets appeal deposit refunded (+60) + initial dispute bond refunded (+60) + task reward (+300)
        # 380 + 60 + 60 + 300 = 800
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 800)

        # Majority honest jurors (juror1 & juror2) get rewarded (+25 points)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 225)
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 225)

        # Minority dishonest juror (juror3) points slashed (-25 points: 200 -> 175)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 175)

        # Verify RewardLedger transaction entries
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_refund').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_slash').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.juror3, transaction_type='juror_slash').exists())

    def test_juror_consensus_appellant_loses_slashing(self):
        # Taker balance: 500 initial - 60 dispute bond - 60 appeal bond = 380
        self.taker_profile.rewards = 380
        self.taker_profile.save()

        appeal = Appeal.objects.create(
            dispute=self.dispute,
            appellant=self.taker,
            appeal_deposit=60,
            status='pending',
            quorum=3,
            consensus_threshold=0.66
        )
        AppealJuror.objects.create(appeal=appeal, juror=self.juror1)
        AppealJuror.objects.create(appeal=appeal, juror=self.juror2)
        AppealJuror.objects.create(appeal=appeal, juror=self.juror3)

        # 2 jurors vote for Poster, 1 for Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'poster'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'poster'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[appeal.id]), {'vote': 'taker'})

        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'resolved')
        self.assertEqual(appeal.ruling, 'overturned')

        # Appellant (Taker) lost: appeal deposit is slashed/forfeited
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_slash').exists())

        # Majority jurors (juror1, juror2) rewarded (+25)
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 225)

        # Minority juror (juror3) slashed (-25)
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 175)

    def test_expired_disputes_command_ignores_active_appeals(self):
        self.dispute.status = 'under_appeal'
        self.dispute.created_at = timezone.now() - timedelta(days=10)
        self.dispute.save()

        call_command('resolve_expired_disputes', days=7)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'under_appeal')


