from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation


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


class TaskDisputeStateMachineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker1_profile = UserProfile.objects.create(user=self.taker1, rewards=1000)

        self.taker2 = User.objects.create_user(username='taker2', password='password123')
        self.taker2_profile = UserProfile.objects.create(user=self.taker2, rewards=1000)

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=100,
            posted_by=self.poster,
            deadline=self.deadline,
            status='available'
        )

        self.client = Client()

    def test_task_model_guard_methods(self):
        # Initial available state
        self.assertTrue(self.task.can_take(self.taker1))
        self.assertFalse(self.task.can_take(self.poster))
        self.assertTrue(self.task.can_cancel())
        self.assertFalse(self.task.can_complete())
        self.assertFalse(self.task.can_abandon())
        self.assertFalse(self.task.can_accept_cancellation())

        # In progress state
        self.task.status = 'in_progress'
        self.task.taken_by = self.taker1
        self.task.save()

        self.assertTrue(self.task.can_complete())
        self.assertTrue(self.task.can_abandon())
        self.assertTrue(self.task.can_request_cancellation())
        self.assertFalse(self.task.can_accept_cancellation())
        self.assertTrue(self.task.can_raise_dispute(self.taker1))

        # Request cancellation
        self.task.cancellation_requested = True
        self.task.save()
        self.assertTrue(self.task.can_accept_cancellation())

        # Disputed state
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Issue")

        self.assertFalse(self.task.can_accept_cancellation())
        self.assertFalse(self.task.can_abandon())
        self.assertTrue(self.task.can_complete())
        self.assertFalse(self.task.can_raise_dispute(self.taker1))

    def test_dispute_can_withdraw_guard(self):
        self.task.status = 'disputed'
        self.task.taken_by = self.taker1
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Issue")

        self.assertTrue(dispute.can_withdraw())

        # Resolved dispute
        dispute.status = 'resolved'
        dispute.save()
        self.assertFalse(dispute.can_withdraw())

        # Task not disputed
        dispute.status = 'open'
        dispute.save()
        self.task.status = 'completed'
        self.task.save()
        self.assertFalse(dispute.can_withdraw())

    def test_reset_to_available_resets_task_state(self):
        self.task.status = 'in_progress'
        self.task.taken_by = self.taker1
        self.task.cancellation_requested = True
        self.task.save()

        self.task.reset_to_available()
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)

    def test_accept_cancellation_on_disputed_task_fails(self):
        """Acceptance Criteria 1: Calling accept_cancellation on a task in disputed status returns error and does not alter records."""
        self.task.status = 'in_progress'
        self.task.taken_by = self.taker1
        self.task.cancellation_requested = True
        self.task.save()
        Conversation.objects.create(task=self.task)

        # Taker raises a dispute -> status becomes disputed
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Issue")
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker1', password='password123')
        response = self.client.get(reverse('accept_cancellation', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertTrue(self.task.cancellation_requested)
        # Verify error message present
        messages_list = list(response.context['messages'])
        self.assertTrue(any("Cancellation cannot be accepted" in str(m) for m in messages_list))

    def test_withdraw_dispute_on_resolved_dispute_or_non_disputed_task_fails(self):
        """Acceptance Criteria 2: Calling withdraw_dispute on a resolved dispute or non-disputed task returns error and does not alter task status."""
        self.task.status = 'disputed'
        self.task.taken_by = self.taker1
        self.task.save()
        Conversation.objects.create(task=self.task)
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason="Issue", status='resolved')

        self.client.login(username='taker1', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        messages_list = list(response.context['messages'])
        self.assertTrue(any("cannot be withdrawn" in str(m) for m in messages_list))

    def test_reopened_task_allows_new_dispute_by_subsequent_taker(self):
        """Acceptance Criteria 3: Re-opened tasks after cancellation or dispute withdrawal can have new disputes raised by subsequent takers."""
        # Step 1: Taker1 takes task, raises dispute, then withdraws dispute
        self.client.login(username='taker1', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Problem'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute1 = self.task.dispute

        self.client.post(reverse('withdraw_dispute', args=[dispute1.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.dispute.status, 'resolved')

        # Taker1 abandons task
        self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')

        # Step 2: Taker2 takes re-opened task and raises a new dispute
        self.client.login(username='taker2', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(self.task.taken_by, self.taker2)

        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'New problem'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker2)
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.reason, 'New problem')

    def test_completing_disputed_task_resolves_dispute_and_prevents_withdrawal(self):
        """Acceptance Criteria 4: Completing a disputed task correctly resolves or cleans up the dispute without enabling retroactive withdrawal manipulation."""
        self.client.login(username='taker1', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Problem'})
        self.task.refresh_from_db()
        dispute = self.task.dispute

        # Poster completes the disputed task
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # Retroactive withdrawal attempt by taker1
        self.client.login(username='taker1', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]), follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        messages_list = list(response.context['messages'])
        self.assertTrue(any("cannot be withdrawn" in str(m) for m in messages_list))
