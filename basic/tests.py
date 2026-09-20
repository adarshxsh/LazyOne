from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Jury, JuryVote, create_jury_for_dispute


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


class JuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Poster & Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # 3 Eligible Neutral Users
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=200)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=300)

        # Ineligible Users
        self.zero_user = User.objects.create_user(username='zero_user', password='password123')
        self.zero_profile = UserProfile.objects.create(user=self.zero_user, rewards=0)

        self.suspended_user = User.objects.create_user(username='suspended_user', password='password123', is_active=False)
        self.suspended_profile = UserProfile.objects.create(user=self.suspended_user, rewards=500)

        # Task
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Jury Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_jury_created_on_dispute_raise(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Task not done right'}
        )
        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury'))
        jury = dispute.jury
        self.assertEqual(jury.jurors.count(), 3)

        juror_ids = list(jury.jurors.values_list('id', flat=True))
        self.assertNotIn(self.poster.id, juror_ids)
        self.assertNotIn(self.taker.id, juror_ids)
        self.assertNotIn(self.zero_user.id, juror_ids)
        self.assertNotIn(self.suspended_user.id, juror_ids)
        self.assertIn(self.juror1.id, juror_ids)
        self.assertIn(self.juror2.id, juror_ids)
        self.assertIn(self.juror3.id, juror_ids)

    def test_poster_and_taker_voting_rejected(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        response_poster = self.client.post(
            reverse('cast_jury_vote', args=[dispute.id]),
            {'vote': 'poster_wins'}
        )
        self.assertEqual(response_poster.status_code, 403)

        self.client.login(username='taker', password='password123')
        response_taker = self.client.post(
            reverse('cast_jury_vote', args=[dispute.id]),
            {'vote': 'taker_wins'}
        )
        self.assertEqual(response_taker.status_code, 403)

    def test_duplicate_voting_prevented(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        res1 = self.client.post(
            reverse('cast_jury_vote', args=[dispute.id]),
            {'vote': 'poster_wins'}
        )
        self.assertRedirects(res1, reverse('dispute_detail', args=[dispute.id]))

        res2 = self.client.post(
            reverse('cast_jury_vote', args=[dispute.id]),
            {'vote': 'taker_wins'}
        )
        self.assertRedirects(res2, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(JuryVote.objects.filter(jury=dispute.jury).count(), 1)

    def test_hidden_tallies_for_jurors_before_voting(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(response.context['show_tallies'])

        self.client.post(
            reverse('cast_jury_vote', args=[dispute.id]),
            {'vote': 'poster_wins'}
        )

        response_after = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertTrue(response_after.context['show_tallies'])

    def test_poster_wins_majority_consensus_and_settlement(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'poster_wins'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'poster_wins'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(dispute.escrow_status, 'forfeited')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        poster_cancellation_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(poster_cancellation_ledger)
        self.assertEqual(poster_cancellation_ledger.amount, 200)

        poster_forfeit_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_refund').first()
        self.assertIsNotNone(poster_forfeit_ledger)
        self.assertEqual(poster_forfeit_ledger.amount, 50)

    def test_taker_wins_majority_consensus_and_settlement(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'taker_wins'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'taker_wins'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.escrow_status, 'refunded')

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 700)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1000)

        completion_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion').first()
        self.assertIsNotNone(completion_ledger)
        self.assertEqual(completion_ledger.amount, 200)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 50)


