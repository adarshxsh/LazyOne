from django.test import TestCase, Client
from django.core.management import call_command
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote


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


class WeightedJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster.date_joined = timezone.now() - timedelta(days=5)
        self.poster.save()
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker.date_joined = timezone.now() - timedelta(days=5)
        self.taker.save()
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Community jurors (non-participants, account age > 24h)
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1.date_joined = timezone.now() - timedelta(days=5)
        self.juror1.save()
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=3500)  # Weight 5

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2.date_joined = timezone.now() - timedelta(days=5)
        self.juror2.save()
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=1800)  # Weight 3

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3.date_joined = timezone.now() - timedelta(days=5)
        self.juror3.save()
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=600)   # Weight 2

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Community Dispute Task",
            description="Task with dispute",
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unclear requirements",
            deposit_amount=100,
            escrow_status='held',
            status='open'
        )

    def test_vote_weight_calculation(self):
        self.assertEqual(self.dispute.get_vote_weight(self.juror1), 5)  # 3500 >= 3000 -> 5
        self.assertEqual(self.dispute.get_vote_weight(self.juror2), 3)  # 1800 >= 1500 -> 3
        self.assertEqual(self.dispute.get_vote_weight(self.juror3), 2)  # 600 >= 500 -> 2

        low_user = User.objects.create_user(username='low_user', password='password123')
        UserProfile.objects.create(user=low_user, rewards=200)
        self.assertEqual(self.dispute.get_vote_weight(low_user), 1)

    def test_voting_eligibility_guards(self):
        # Participants cannot vote
        self.assertFalse(self.dispute.can_user_vote(self.poster))
        self.assertFalse(self.dispute.can_user_vote(self.taker))

        # Eligible community juror
        self.assertTrue(self.dispute.can_user_vote(self.juror1))

        # User with rewards < 100 cannot vote
        poor_juror = User.objects.create_user(username='poor_juror', password='password123')
        poor_juror.date_joined = timezone.now() - timedelta(days=5)
        poor_juror.save()
        UserProfile.objects.create(user=poor_juror, rewards=50)
        self.assertFalse(self.dispute.can_user_vote(poor_juror))

        # User created < 24h cannot vote
        new_juror = User.objects.create_user(username='new_juror', password='password123')
        UserProfile.objects.create(user=new_juror, rewards=1000)
        self.assertFalse(self.dispute.can_user_vote(new_juror))

    def test_open_dispute_view_authorization(self):
        # Open dispute viewable by community juror
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Non-participant viewing resolved dispute gets redirected
        self.dispute.status = 'resolved'
        self.dispute.save()

        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_weighted_quorum_consensus_poster_win(self):
        # Juror1 (W=5) votes poster
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'vote_choice': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')  # Quorum not met yet (W=5 < 10, N=1 < 3)

        # Juror2 (W=3) votes poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'vote_choice': 'poster'})
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')  # Quorum not met yet (W=8 < 10, N=2 < 3)

        # Juror3 (W=2) votes taker
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'vote_choice': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Quorum (W=10 >= 10, N=3 >= 3) and Poster consensus (8/10 = 80% >= 60%) reached!
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster gets task reward (500) refunded + deposit bond forfeited from taker (100) = 1000 + 500 + 100 = 1600
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1600)

        # Winning jurors (Juror1 and Juror2) get 10 pts micro-reward
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 3510)
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 1810)

        # Losing juror (Juror3) rewards remain 600
        self.juror3_profile.refresh_from_db()
        self.assertEqual(self.juror3_profile.rewards, 600)

    def test_expired_dispute_resolution_with_votes(self):
        # Create votes: Juror1 (W=5) votes taker, Juror2 (W=3) votes poster
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror1, vote_choice='taker', weight=5)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.juror2, vote_choice='poster', weight=3)

        # Set dispute creation to 8 days ago
        self.dispute.created_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        # Run expiration command
        call_command('resolve_expired_disputes', days=7)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Taker leading (W=5 vs W=3), so dispute resolves in favor of Taker
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Taker gets reward (500) + deposit refund (100)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1600)

        # Taker juror (Juror1) gets 10 pts micro-reward
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 3510)

