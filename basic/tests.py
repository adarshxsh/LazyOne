from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation
from .dispute_logic import is_eligible_juror, evaluate_dispute_state, file_appeal, final_settlement


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
        self.assertEqual(dispute.status, 'voting_primary')
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

        dispute = Dispute.objects.get(task=self.task)

        # Taker withdraws dispute
        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 100 + 300 (task reward) = 400
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


class TieredDisputeSystemTestCase(TestCase):

    def setUp(self):
        self.client = Client()

        # Users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.friend_of_poster = User.objects.create_user(username='friend_poster', password='password123')
        self.friend_of_taker = User.objects.create_user(username='friend_taker', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        self.friend_poster_profile, _ = UserProfile.objects.get_or_create(user=self.friend_of_poster, defaults={'rewards': 1000})
        self.friend_taker_profile, _ = UserProfile.objects.get_or_create(user=self.friend_of_taker, defaults={'rewards': 1000})

        # Add Friendships
        self.poster_profile.friends.add(self.friend_poster_profile)
        self.friend_poster_profile.friends.add(self.poster_profile)

        self.taker_profile.friends.add(self.friend_taker_profile)
        self.friend_taker_profile.friends.add(self.taker_profile)

        # Create Juror Users
        self.jurors = []
        for i in range(10):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            p, _ = UserProfile.objects.get_or_create(user=u, defaults={'rewards': 1000})
            self.jurors.append(u)

        # Task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

    def test_dispute_model_choices_and_ledger_types(self):
        # Check Dispute status choices
        dispute_statuses = dict(Dispute.STATUS_CHOICES)
        self.assertIn('open', dispute_statuses)
        self.assertIn('voting_primary', dispute_statuses)
        self.assertIn('appeal_window', dispute_statuses)
        self.assertIn('appeal_pending', dispute_statuses)
        self.assertIn('resolved', dispute_statuses)
        self.assertIn('escalated_staff', dispute_statuses)

        # Check RewardLedger transaction types
        ledger_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('litigant_slashing', ledger_types)
        self.assertIn('juror_slashing', ledger_types)
        self.assertIn('appeal_bond', ledger_types)
        self.assertIn('governance_reward', ledger_types)

    def test_participant_isolation(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Poster refuses to verify',
            status='voting_primary'
        )

        # Poster, taker, and friends of poster/taker should NOT be eligible
        self.assertFalse(is_eligible_juror(self.poster, dispute))
        self.assertFalse(is_eligible_juror(self.taker, dispute))
        self.assertFalse(is_eligible_juror(self.friend_of_poster, dispute))
        self.assertFalse(is_eligible_juror(self.friend_of_taker, dispute))

        # Unrelated jurors SHOULD be eligible
        self.assertTrue(is_eligible_juror(self.jurors[0], dispute))

    def test_primary_jury_voting_and_quorum_success(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute over completion',
            status='voting_primary',
            primary_voting_ends_at=timezone.now() + timedelta(hours=48)
        )
        self.task.status = 'disputed'
        self.task.save()

        # 3 Jurors vote (min primary quorum = 3): 2 taker_wins, 1 poster_wins
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[0], vote='taker_wins', stage='primary')
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[1], vote='taker_wins', stage='primary')
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[2], vote='poster_wins', stage='primary')

        evaluate_dispute_state(dispute)
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'appeal_window')
        self.assertEqual(dispute.primary_winner, 'taker_wins')
        self.assertIsNotNone(dispute.appeal_window_expires_at)

    def test_primary_jury_quorum_failure_escalates_to_staff(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            status='voting_primary',
            primary_voting_ends_at=timezone.now() - timedelta(hours=1) # expired
        )

        # Only 2 votes (below quorum of 3)
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[0], vote='taker_wins', stage='primary')
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[1], vote='taker_wins', stage='primary')

        evaluate_dispute_state(dispute)
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'escalated_staff')

    def test_complete_task_blocked_during_dispute(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            status='voting_primary'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(f'/task/complete/{self.task.id}/')

        # Should be redirected to dispute detail and task status remains disputed
        self.assertRedirects(response, f'/dispute/{dispute.id}/')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

    def test_appeal_filing_and_bond_deduction(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            status='appeal_window',
            primary_winner='taker_wins',
            appeal_window_expires_at=timezone.now() + timedelta(hours=24)
        )

        # Poster files appeal (20% of 100 reward = 20 points)
        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/appeal/{dispute.id}/')

        self.assertRedirects(response, f'/dispute/{dispute.id}/')
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'appeal_pending')
        self.assertEqual(dispute.appellant, self.poster)
        self.assertEqual(dispute.appeal_bond_amount, 20)

        # Poster rewards reduced from 1000 to 980
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 980)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_bond').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -20)

    def test_appeal_jury_supermajority_settlement_and_slashing(self):
        # Set up dispute in appeal_pending
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            status='appeal_pending',
            primary_winner='taker_wins',
            appellant=self.poster,
            appeal_bond_amount=20,
            appeal_voting_ends_at=timezone.now() + timedelta(hours=48)
        )
        self.task.status = 'disputed'
        self.task.save()

        # Poster previously paid 20 points bond
        self.poster_profile.rewards = 980
        self.poster_profile.save()

        # Tier-2 Jury: 7 Jurors (min quorum = 7).
        # 5 vote poster_wins (5/7 = 71.4% >= 66% supermajority), 2 vote taker_wins.
        for i in range(5):
            DisputeVote.objects.create(dispute=dispute, juror=self.jurors[i], vote='poster_wins', stage='appeal')
        for i in range(5, 7):
            DisputeVote.objects.create(dispute=dispute, juror=self.jurors[i], vote='taker_wins', stage='appeal')

        evaluate_dispute_state(dispute)
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.final_winner, 'poster_wins')

        # Task settled: poster wins, task refunded & cancelled
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Check Appellant (Poster) won appeal:
        # 1. Escrow refunded: +100 reward
        # 2. Appeal bond refunded: +20 points
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 980 + 100 + 20) # 1100

        # Check Losing Litigant (Taker) slashed:
        # Litigant slash penalty: 20% of 100 = 20 points
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1000 - 20) # 980

        # Check Unaligned Jurors slashed (jurors 5 & 6 voted taker_wins):
        # Juror slash penalty: 5% of 100 = 5 points each
        for i in range(5, 7):
            p = UserProfile.objects.get(user=self.jurors[i])
            self.assertEqual(p.rewards, 1000 - 5)

        # Check Aligned Jurors (jurors 0..4) received governance rewards:
        # Total pool = losing litigant slash (20) + unaligned juror slash (5*2 = 10) = 30 points.
        # Distributed among 5 aligned jurors = 30 // 5 = 6 points each.
        for i in range(5):
            p = UserProfile.objects.get(user=self.jurors[i])
            self.assertEqual(p.rewards, 1000 + 6)

        # Check RewardLedger entries
        self.assertTrue(RewardLedger.objects.filter(transaction_type='litigant_slashing').exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='juror_slashing').exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='governance_reward').exists())

    def test_unappealed_dispute_settlement(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason',
            status='appeal_window',
            primary_winner='taker_wins',
            appeal_window_expires_at=timezone.now() - timedelta(minutes=1) # Expired window
        )
        self.task.status = 'disputed'
        self.task.save()

        # Primary votes: jurors 0, 1 voted taker_wins, juror 2 voted poster_wins
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[0], vote='taker_wins', stage='primary')
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[1], vote='taker_wins', stage='primary')
        DisputeVote.objects.create(dispute=dispute, juror=self.jurors[2], vote='poster_wins', stage='primary')

        evaluate_dispute_state(dispute)
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.final_winner, 'taker_wins')

        # Taker awarded task reward: 1000 + 100 = 1100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1100)

        # Poster slashed for losing dispute: 1000 - 20 = 980
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 980)

