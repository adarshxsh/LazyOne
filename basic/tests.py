import math
from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
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


class PartialDisputeSettlementTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        
        # Create doer/taker
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.doer_profile = UserProfile.objects.create(user=self.doer, rewards=500)

        # Create bystander
        self.bystander = User.objects.create_user(username='bystander', password='password123')
        self.bystander_profile = UserProfile.objects.create(user=self.bystander, rewards=1000)

        # Create a task posted by poster, taken by doer
        self.task = Task.objects.create(
            title="Build Partial Dispute Feature",
            description="Implement escrow split logic",
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )

        # Create conversation for taken task
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.doer)

        # Poster's balance reduced by 100 on task creation
        self.poster_profile.rewards -= 100
        self.poster_profile.save()
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-100,
            transaction_type='task_creation', description="Reserved for task"
        )

        # Raise dispute by doer
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason="Work partially completed, need to split reward."
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_propose_settlement_valid(self):
        self.client.login(username='doer', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url, {'proposed_taker_pct': 60}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposed_taker_pct, 60)
        self.assertEqual(self.dispute.proposed_by, self.doer)
        self.assertEqual(self.dispute.proposal_status, 'pending')

        # Notification to poster
        noti = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(noti)
        self.assertIn("60% to taker", noti.message)

    def test_propose_settlement_invalid_percentages(self):
        self.client.login(username='doer', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])

        invalid_percentages = [0, 100, -10, 150, "invalid"]
        for invalid_pct in invalid_percentages:
            response = self.client.post(url, {'proposed_taker_pct': invalid_pct}, follow=True)
            self.dispute.refresh_from_db()
            self.assertEqual(self.dispute.proposal_status, 'none')
            self.assertIsNone(self.dispute.proposed_taker_pct)

    def test_propose_settlement_unauthorized(self):
        self.client.login(username='bystander', password='password123')
        url = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url, {'proposed_taker_pct': 50})

        self.assertRedirects(response, reverse('home'))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposal_status, 'none')

    def test_decline_settlement_and_counter_offer(self):
        # Doer proposes 80%
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 80})

        # Poster logs in and declines
        self.client.login(username='poster', password='password123')
        url_respond = reverse('respond_settlement', args=[self.dispute.id])
        response = self.client.post(url_respond, {'action': 'decline'}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposal_status, 'declined')

        # Notification to doer
        noti = Notification.objects.filter(recipient=self.doer).first()
        self.assertIsNotNone(noti)
        self.assertIn("declined", noti.message)

        # Poster makes counter-offer of 50%
        url_propose = reverse('propose_settlement', args=[self.dispute.id])
        response = self.client.post(url_propose, {'proposed_taker_pct': 50}, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.proposed_taker_pct, 50)
        self.assertEqual(self.dispute.proposed_by, self.poster)
        self.assertEqual(self.dispute.proposal_status, 'pending')

    def test_accept_settlement_atomic_execution(self):
        # Doer proposes 60%
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 60})

        initial_doer_rewards = self.doer_profile.rewards
        initial_poster_rewards = self.poster_profile.rewards

        # Poster accepts 60% offer
        self.client.login(username='poster', password='password123')
        url_respond = reverse('respond_settlement', args=[self.dispute.id])
        response = self.client.post(url_respond, {'action': 'accept'}, follow=True)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.proposal_status, 'accepted')
        self.assertEqual(self.task.status, 'completed')

        # Balance updates: 60 points to doer, 40 points to poster
        self.assertEqual(self.doer_profile.rewards, initial_doer_rewards + 60)
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 40)

        # RewardLedger entries check
        payout_ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_partial_payout').first()
        refund_ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_partial_refund').first()

        self.assertIsNotNone(payout_ledger)
        self.assertEqual(payout_ledger.user, self.doer)
        self.assertEqual(payout_ledger.amount, 60)

        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.user, self.poster)
        self.assertEqual(refund_ledger.amount, 40)

        # Total points disbursed equals total task reward
        self.assertEqual(payout_ledger.amount + refund_ledger.amount, self.task.reward)

    def test_math_precision_and_conservation_odd_splits(self):
        # Task with 33 points reward
        odd_task = Task.objects.create(
            title="Odd reward task",
            description="33 reward points",
            reward=33,
            posted_by=self.poster,
            taken_by=self.doer,
            status='in_progress'
        )
        odd_conversation = Conversation.objects.create(task=odd_task)
        odd_conversation.participants.add(self.poster, self.doer)

        odd_dispute = Dispute.objects.create(
            task=odd_task, raised_by=self.doer, reason="Odd split"
        )
        odd_task.status = 'disputed'
        odd_task.save()

        # Propose 40% split (floor(33 * 0.40) = 13, poster gets 33 - 13 = 20)
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[odd_dispute.id]), {'proposed_taker_pct': 40})

        initial_doer_rewards = self.doer_profile.rewards
        initial_poster_rewards = self.poster_profile.rewards

        self.client.login(username='poster', password='password123')
        self.client.post(reverse('respond_settlement', args=[odd_dispute.id]), {'action': 'accept'})

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()

        doer_gain = self.doer_profile.rewards - initial_doer_rewards
        poster_gain = self.poster_profile.rewards - initial_poster_rewards

        self.assertEqual(doer_gain, 13)
        self.assertEqual(poster_gain, 20)
        self.assertEqual(doer_gain + poster_gain, 33)

    def test_state_locking_complete_task_while_proposal_pending(self):
        # Doer proposes settlement
        self.client.login(username='doer', password='password123')
        self.client.post(reverse('propose_settlement', args=[self.dispute.id]), {'proposed_taker_pct': 50})

        # Poster attempts to call complete_task while proposal is pending
        self.client.login(username='poster', password='password123')
        url_complete = reverse('complete_task', args=[self.task.id])
        response = self.client.get(url_complete, follow=True)

        self.task.refresh_from_db()
        self.assertNotEqual(self.task.status, 'completed')
        self.assertEqual(self.task.status, 'disputed')
        messages_list = [m.message for m in response.context['messages']]
        self.assertTrue(any("Cannot complete task while a settlement proposal is pending" in m for m in messages_list))
