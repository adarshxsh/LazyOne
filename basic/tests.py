from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification


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


class DisputeResolutionAndAppealTests(TestCase):
    def setUp(self):
        # Create users and profiles
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})

        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1000})

        self.juror = User.objects.create_user(username='juror', password='password123')
        self.juror_profile, _ = UserProfile.objects.get_or_create(user=self.juror, defaults={'rewards': 1000, 'is_juror': True})

        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.regular_profile, _ = UserProfile.objects.get_or_create(user=self.regular_user, defaults={'rewards': 1000})

        # Create task and dispute
        self.task = Task.objects.create(
            title="Test Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but not accepted",
            status='open'
        )

        self.client = Client()

    def test_resolve_dispute_by_staff_taker_wins(self):
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'winner': self.taker.id,
            'resolution_reason': 'Evidence supports task taker'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.taker)
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 700) # 500 initial + 200 reward

        # Verify ledger entry
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Verify notifications sent to both parties
        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        taker_notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(poster_notif)
        self.assertIsNotNone(taker_notif)

    def test_resolve_dispute_by_juror_poster_wins(self):
        self.client.login(username='juror', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'winner': 'posted_by',
            'resolution_reason': 'Task requirement not fulfilled'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.poster)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 initial + 200 refund

        # Verify ledger entry
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

    def test_resolve_dispute_unauthorized(self):
        self.client.login(username='regular', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'winner': self.taker.id,
            'resolution_reason': 'Unauthorized resolution attempt'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]), fetch_redirect_response=False)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_appeal_dispute_within_48_hours(self):
        # Resolve dispute first
        self.dispute.status = 'resolved'
        self.dispute.winner = self.taker
        self.dispute.resolved_at = timezone.now() - timedelta(hours=2)
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        url = reverse('appeal_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'reason': 'I have new evidence proving incomplete work.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertEqual(self.dispute.appealed_by, self.poster)
        self.assertEqual(self.dispute.appeal_reason, 'I have new evidence proving incomplete work.')

        # Verify notifications sent to both parties
        poster_notif = Notification.objects.filter(recipient=self.poster, message__contains='filed an appeal').first()
        taker_notif = Notification.objects.filter(recipient=self.taker, message__contains='filed an appeal').first()
        self.assertIsNotNone(poster_notif)
        self.assertIsNotNone(taker_notif)

    def test_appeal_dispute_after_48_hours(self):
        # Resolve dispute 50 hours ago
        self.dispute.status = 'resolved'
        self.dispute.winner = self.taker
        self.dispute.resolved_at = timezone.now() - timedelta(hours=50)
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        url = reverse('appeal_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'reason': 'Late appeal submission'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_appeal_dispute_unauthorized_user(self):
        self.dispute.status = 'resolved'
        self.dispute.winner = self.taker
        self.dispute.resolved_at = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        self.client.login(username='regular', password='password123')
        url = reverse('appeal_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'reason': 'Non-party trying to appeal'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]), fetch_redirect_response=False)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_resolve_appealed_dispute_change_winner(self):
        # Initial resolution gave taker 200 points
        self.dispute.status = 'resolved'
        self.dispute.winner = self.taker
        self.dispute.resolved_at = timezone.now() - timedelta(hours=1)
        self.dispute.save()
        self.taker_profile.rewards = 700
        self.taker_profile.save()

        # Poster appeals
        self.dispute.status = 'appealed'
        self.dispute.appealed_by = self.poster
        self.dispute.appeal_reason = 'New evidence'
        self.dispute.save()

        # Senior arbitrator re-resolves in favor of poster
        self.client.login(username='staff', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'winner': 'posted_by',
            'resolution_reason': 'Appeal upheld for poster'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.poster)
        self.assertEqual(self.taker_profile.rewards, 500) # Reclaimed 200
        self.assertEqual(self.poster_profile.rewards, 1200) # Awarded 200

        # Verify ledger reversal and award entries
        taker_reversal = RewardLedger.objects.filter(user=self.taker, amount=-200).first()
        poster_award = RewardLedger.objects.filter(user=self.poster, amount=200).first()
        self.assertIsNotNone(taker_reversal)
        self.assertIsNotNone(poster_award)
