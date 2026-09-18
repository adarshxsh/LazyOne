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


class DisputeAdjudicationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.random_user = User.objects.create_user(username='random', password='password123')

        # Create profiles with initial points
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1500})
        self.random_profile, _ = UserProfile.objects.get_or_create(user=self.random_user, defaults={'rewards': 1500})

        # Create a task posted by poster and taken by taker
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=2),
            status='disputed'
        )

        # Create Conversation for task to avoid NoReverseMatch in home template rendering
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create an open dispute for the task raised by taker
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Incomplete work claim",
            status='open'
        )

    def test_non_staff_cannot_adjudicate_dispute(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'payout_taker'})
        self.assertEqual(response.status_code, 403)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_adjudication_payout_taker(self):
        self.client.login(username='staff', password='password123')
        initial_taker_rewards = self.taker_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'payout_taker'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_ruling, 'payout_taker')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertIsNotNone(self.dispute.resolved_at)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_adjudication_refund_poster(self):
        self.client.login(username='staff', password='password123')
        initial_poster_rewards = self.poster_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {'ruling': 'refund_poster'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_ruling, 'refund_poster')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 200)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)

    def test_staff_adjudication_split(self):
        self.client.login(username='staff', password='password123')
        initial_poster_rewards = self.poster_profile.rewards
        initial_taker_rewards = self.taker_profile.rewards

        response = self.client.post(f'/dispute/{self.dispute.id}/resolve/', {
            'ruling': 'split',
            'poster_amount': 120,
            'taker_amount': 80
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 120)
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 80)

    def test_appeal_filing_by_taker(self):
        # First resolve the dispute
        self.dispute.status = 'resolved'
        self.dispute.resolution_ruling = 'refund_poster'
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.assertTrue(self.dispute.is_appealable)

        # Login as taker and submit appeal
        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'New evidence shows work was submitted on time.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'pending')
        self.assertEqual(self.dispute.appeal_filed_by, self.taker)
        self.assertEqual(self.dispute.status, 'under_appeal')
        self.assertEqual(self.dispute.appeal_reason, 'New evidence shows work was submitted on time.')

        # Verify staff notification
        self.assertTrue(Notification.objects.filter(recipient=self.staff_user).exists())

    def test_appeal_filing_unauthorized_user_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        self.client.login(username='random', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Random appeal'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.appeal_status, 'pending')

    def test_appeal_filing_outside_time_window_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now() - timedelta(days=8)
        self.dispute.save()

        self.assertFalse(self.dispute.is_appealable)

        self.client.login(username='taker', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Late appeal'
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.appeal_status, 'pending')

    def test_duplicate_appeal_blocked(self):
        self.dispute.status = 'resolved'
        self.dispute.resolved_at = timezone.now()
        self.dispute.appeal_status = 'pending'
        self.dispute.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/', {
            'appeal_reason': 'Second appeal attempt'
        }, follow=True)

        # Status shouldn't change
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'pending')

    def test_staff_review_appeal_confirm(self):
        self.dispute.status = 'under_appeal'
        self.dispute.appeal_status = 'pending'
        self.dispute.appeal_reason = 'Testing review'
        self.dispute.save()

        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/review/', {
            'action': 'reject',
            'notes': 'Original decision confirmed.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'rejected')
        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.dispute.appeal_resolution_notes, 'Original decision confirmed.')

    def test_staff_review_appeal_overturn(self):
        # Set initial resolution: refund_poster
        self.dispute.resolution_ruling = 'refund_poster'
        self.dispute.status = 'under_appeal'
        self.dispute.appeal_status = 'pending'
        self.dispute.appeal_reason = 'Valid appeal'
        self.dispute.save()

        # Poster had received refund points
        self.poster_profile.rewards = 1700
        self.poster_profile.save()

        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/{self.dispute.id}/appeal/review/', {
            'action': 'approve',
            'corrective_action': 'payout_taker',
            'notes': 'Overturned in favor of taker.'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.appeal_status, 'approved')
        self.assertEqual(self.dispute.status, 'appeal_closed')
        self.assertEqual(self.poster_profile.rewards, 1500)
        self.assertEqual(self.taker_profile.rewards, 1700)
