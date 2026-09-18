from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminDisputeAndAppealsTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_admin = User.objects.create_user(username='staff_admin', password='password123', is_staff=True)

        # Create profiles
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_admin, defaults={'rewards': 1500})

        # Create task and conversation
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work was completed but poster refused payout",
            status='open'
        )

    def test_staff_adjudication_poster_award(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'decision': 'poster',
            'rationale': 'Task was incomplete'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_decision, 'poster')
        self.assertEqual(self.dispute.poster_payout, 100)
        self.assertEqual(self.dispute.taker_payout, 0)
        self.assertEqual(self.dispute.resolved_by, self.staff_admin)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1600)  # 1500 + 100

        # Verify ledger entry
        ledger = RewardLedger.objects.get(user=self.poster, task=self.task, transaction_type='dispute_refund')
        self.assertEqual(ledger.amount, 100)

        # Verify notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_adjudication_taker_award(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'decision': 'taker',
            'rationale': 'Work was satisfactory'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_decision, 'taker')
        self.assertEqual(self.dispute.poster_payout, 0)
        self.assertEqual(self.dispute.taker_payout, 100)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1600)  # 1500 + 100

        # Verify ledger entry
        ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='dispute_payout')
        self.assertEqual(ledger.amount, 100)

    def test_staff_adjudication_split_award(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'decision': 'split',
            'taker_percent': '60',
            'rationale': 'Partial completion'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.poster_payout, 40)
        self.assertEqual(self.dispute.taker_payout, 60)
        self.assertEqual(self.poster_profile.rewards, 1540)
        self.assertEqual(self.taker_profile.rewards, 1560)

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'decision': 'poster',
            'rationale': 'Self award'
        })
        self.assertEqual(response.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_file_appeal_within_72_hours(self):
        # Resolve dispute first
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now()
        self.dispute.poster_payout = 100
        self.dispute.taker_payout = 0
        self.dispute.resolution_decision = 'poster'
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        url = reverse('file_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'justification': 'Evidence provided proves completion.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'pending_appeal')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_justification, 'Evidence provided proves completion.')

    def test_file_appeal_outside_72_hours_fails(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now() - timedelta(hours=73)
        self.dispute.save()

        self.client.login(username='taker', password='password123')
        url = reverse('file_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'justification': 'Late appeal submission'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_staff_appellate_review_uphold(self):
        self.dispute.status = 'pending_appeal'
        self.dispute.resolved_at = timezone.now() - timedelta(hours=2)
        self.dispute.appealed_at = timezone.now() - timedelta(hours=1)
        self.dispute.poster_payout = 100
        self.dispute.taker_payout = 0
        self.dispute.save()

        self.client.login(username='staff_admin', password='password123')
        url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'appeal_decision': 'uphold',
            'rationale': 'Original decision was sound.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.dispute.appeal_decision, 'uphold')

    def test_staff_appellate_review_reverse(self):
        self.dispute.status = 'pending_appeal'
        self.dispute.resolved_at = timezone.now() - timedelta(hours=2)
        self.dispute.appealed_at = timezone.now() - timedelta(hours=1)
        self.dispute.resolution_decision = 'poster'
        self.dispute.poster_payout = 100
        self.dispute.taker_payout = 0
        self.dispute.save()

        # Give poster the initial points to simulate initial resolution
        self.poster_profile.rewards = 1600
        self.poster_profile.save()

        self.client.login(username='staff_admin', password='password123')
        url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'appeal_decision': 'reverse',
            'rationale': 'New evidence shows taker completed work.'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.dispute.appeal_decision, 'reverse')
        self.assertEqual(self.dispute.poster_payout, 0)
        self.assertEqual(self.dispute.taker_payout, 100)
        self.assertEqual(self.poster_profile.rewards, 1500)  # 1600 - 100
        self.assertEqual(self.taker_profile.rewards, 1600)   # 1500 + 100
        self.assertEqual(self.task.status, 'completed')

        # Check ledger entries for reversal
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='appeal_reversal', amount=-100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_payout', amount=100).exists())
