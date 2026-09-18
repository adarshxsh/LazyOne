from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from .models import UserProfile, Task, Dispute, DisputeAppeal, RewardLedger, Conversation, Notification


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


class StaffDisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='admin', password='password123', is_staff=True)

        # UserProfiles created or ensured
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 500})

        # Set up a task and raise dispute
        self.task = Task.objects.create(
            title="Clean standard dorm",
            description="Clean room 101",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Poster did not approve task completion."
        )

        self.client = Client()

    def test_staff_arbitration_poster_wins(self):
        self.client.login(username='admin', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'poster', 'note': 'Task was incomplete'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        # Check atomic updates
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.poster)
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'resolved')

        # Check points refund
        self.assertEqual(self.poster_profile.rewards, 1100)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

        # Check Notifications generated for both participants
        notifs = Notification.objects.filter(link=reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(notifs.count(), 2)

    def test_staff_arbitration_taker_wins(self):
        self.client.login(username='admin', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'taker', 'note': 'Work verified'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'resolved')
        self.assertEqual(self.taker_profile.rewards, 600)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 100)

    def test_non_staff_cannot_arbitrate(self):
        self.client.login(username='taker', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'winner': 'taker', 'note': 'Self win'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertIsNone(self.dispute.winner)

    def test_single_tier_appeal_submission(self):
        # First, arbitrate as staff
        self.client.login(username='admin', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster', 'note': 'Initial ruling'})

        # Now taker appeals
        self.client.login(username='taker', password='password123')
        appeal_url = reverse('submit_appeal', args=[self.dispute.id])
        response = self.client.post(appeal_url, {'reason': 'Photo evidence shows room cleaned thoroughly.'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertTrue(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).exists())

        # Attempt duplicate appeal submission by same user
        response_dup = self.client.post(appeal_url, {'reason': 'Second appeal attempt'})
        self.assertEqual(response_dup.status_code, 302)
        self.assertEqual(DisputeAppeal.objects.filter(dispute=self.dispute, appellant=self.taker).count(), 1)

    def test_appeal_window_expired(self):
        # Arbitrate dispute
        self.client.login(username='admin', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster'})

        # Refresh from DB and set resolved_at to 3 days ago
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.dispute.resolved_at = timezone.now() - timedelta(days=3)
        self.dispute.save()

        # Taker attempts to appeal after window
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {'reason': 'Late appeal'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertFalse(DisputeAppeal.objects.filter(dispute=self.dispute).exists())

    def test_secondary_staff_review_uphold(self):
        # Arbitrate and submit appeal
        self.dispute.status = 'resolved'
        self.dispute.winner = self.poster
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        DisputeAppeal.objects.create(dispute=self.dispute, appellant=self.taker, reason="Needs review")
        self.dispute.status = 'appealed'
        self.dispute.save()

        # Staff reviews appeal and upholds
        self.client.login(username='admin', password='password123')
        review_url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(review_url, {'verdict': 'uphold', 'note': 'Initial ruling confirmed'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'finalized')
        self.assertEqual(self.dispute.final_verdict, 'uphold')
        self.assertEqual(self.dispute.winner, self.poster)

    def test_secondary_staff_review_reverse(self):
        # Arbitrate giving points to poster (+100 to poster)
        self.poster_profile.rewards += 100
        self.poster_profile.save()

        self.dispute.status = 'resolved'
        self.dispute.winner = self.poster
        self.dispute.resolved_by = self.staff_user
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        DisputeAppeal.objects.create(dispute=self.dispute, appellant=self.taker, reason="Reversal evidence")
        self.dispute.status = 'appealed'
        self.dispute.save()

        initial_poster_rewards = self.poster_profile.rewards
        initial_taker_rewards = self.taker_profile.rewards

        # Staff reviews appeal and reverses decision
        self.client.login(username='admin', password='password123')
        review_url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(review_url, {'verdict': 'reverse', 'note': 'Reversing decision based on appeal'})
        self.assertEqual(response.status_code, 302)

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'finalized')
        self.assertEqual(self.dispute.final_verdict, 'reverse')
        self.assertEqual(self.dispute.winner, self.taker)

        # Points transferred from poster to taker
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards - 100)
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 100)

        # Verify dispute is finalized and locked from further appeal/arbitration
        resp_re_arbitrate = self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {'winner': 'poster'})
        self.assertEqual(resp_re_arbitrate.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'finalized')

    def test_staff_disputes_dashboard_access(self):
        self.client.login(username='admin', password='password123')
        response = self.client.get(reverse('staff_disputes_list'))
        self.assertEqual(response.status_code, 200)

        self.client.login(username='poster', password='password123')
        response_non_staff = self.client.get(reverse('staff_disputes_list'))
        self.assertEqual(response_non_staff.status_code, 302)
