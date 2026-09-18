from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
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


class DisputeAndAbandonmentTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_both_poster_and_taker_can_raise_dispute(self):
        # Taker raises dispute
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Taker dispute reason'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        self.assertEqual(self.task.dispute.reason, 'Taker dispute reason')

        # Clean up dispute for poster test
        self.task.dispute.delete()
        self.task.status = 'in_progress'
        self.task.save()

        # Poster raises dispute
        response = self.client_poster.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Poster dispute reason'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Poster dispute reason')

    def test_unauthorized_user_cannot_raise_dispute(self):
        response = self.client_other.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unauthorized dispute'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_task_abandonment_blocked_during_active_dispute(self):
        # Raise dispute first
        Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        initial_taker_rewards = self.taker.userprofile.rewards

        # Taker attempts to abandon task
        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.task.taken_by, self.taker)

        # Check explicit error message in response context/messages
        messages = list(response.context['messages']) if response.context and 'messages' in response.context else []
        message_texts = [str(m) for m in messages]
        self.assertTrue(any('blocked' in m.lower() or 'dispute' in m.lower() for m in message_texts))

        # Ensure no points were deducted and no abandonment ledger record created
        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards)
        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').exists())

    def test_abandon_undisputed_task_deducts_penalty_and_creates_ledger(self):
        initial_rewards = self.taker.userprofile.rewards
        penalty = getattr(settings, 'ABANDONMENT_PENALTY', 50)

        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_rewards - penalty)

        ledger_entry = RewardLedger.objects.get(
            user=self.taker,
            task=self.task,
            transaction_type='task_abandonment'
        )
        self.assertEqual(ledger_entry.amount, -penalty)

    def test_abandonment_penalty_deduction_allows_negative_balance(self):
        profile = self.taker.userprofile
        profile.rewards = 20
        profile.save()

        penalty = getattr(settings, 'ABANDONMENT_PENALTY', 50)

        response = self.client_taker.get(reverse('abandon_task', args=[self.task.id]), follow=True)

        profile.refresh_from_db()
        self.assertEqual(profile.rewards, 20 - penalty)
        self.assertLess(profile.rewards, 0)

    def test_poster_initiated_mutual_cancellation_has_no_taker_penalty(self):
        initial_taker_rewards = self.taker.userprofile.rewards

        # Poster requests cancellation
        self.client_poster.get(reverse('request_cancellation', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertTrue(self.task.cancellation_requested)

        # Taker accepts cancellation
        self.client_taker.get(reverse('accept_cancellation', args=[self.task.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)

        # Taker balance should remain unchanged
        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards)

        # No abandonment penalty ledger entry for taker
        self.assertFalse(RewardLedger.objects.filter(user=self.taker, transaction_type='task_abandonment').exists())
