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


class DisputeAppealTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=100)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=20)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=100)

        self.task = Task.objects.create(
            title="Appeal Test Task",
            description="Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Unresolved dispute",
            deposit_amount=100,
            escrow_status='held',
            status='open'
        )

    def test_file_appeal_success_and_ledger(self):
        self.client.login(username='taker', password='password123')

        # Appeal fee for deposit_amount=100 is math.ceil(100 * 1.5) = 150
        self.assertEqual(self.dispute.required_appeal_fee, 150)

        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertEqual(self.dispute.appellant, self.taker)
        self.assertEqual(self.dispute.appeal_fee, 150)

        # Balance check: 500 - 150 = 350
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 350)

        # Ledger check for 'appeal_fee'
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_fee').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -150)

    def test_file_appeal_insufficient_rewards(self):
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_file_appeal_non_litigant_denied(self):
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]), fetch_redirect_response=False)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_file_appeal_window_expired(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now() - timedelta(hours=49)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.status, 'appealed')

    def test_juror_voting_and_stake_slashing(self):
        # 1. File appeal
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.dispute.jurors.set([self.juror1, self.juror2, self.juror3])

        # 2. Juror 1 votes for Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[self.dispute.id]), {'voted_for_id': self.taker.id})

        # 3. Juror 2 votes for Poster (minority voter!)
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[self.dispute.id]), {'voted_for_id': self.poster.id})

        # 4. Juror 3 votes for Taker (majority 2/3 reached!)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_juror_vote', args=[self.dispute.id]), {'voted_for_id': self.taker.id})

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appeal_resolved')

        # Check that Juror 2 (minority voter) was slashed
        # Juror 2 initial rewards = 20. Slashing penalty = 50. Capped at 20 -> balance 0.
        self.juror2_profile.refresh_from_db()
        self.assertEqual(self.juror2_profile.rewards, 0)

        # Check juror_slashing transaction ledger entry
        slashing_ledger = RewardLedger.objects.filter(user=self.juror2, transaction_type='juror_slashing').first()
        self.assertIsNotNone(slashing_ledger)
        self.assertEqual(slashing_ledger.amount, -20)


