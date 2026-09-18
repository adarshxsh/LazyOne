from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Conversation


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
        self.assertEqual(dispute.status, 'withdrawn')

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


class DisputeStateMachineTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.stranger = User.objects.create_user(username='stranger', password='password123')
        
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=1000)
        UserProfile.objects.create(user=self.taker, rewards=500)
        UserProfile.objects.create(user=self.stranger, rewards=500)

        UserProfile.objects.create(user=self.juror1, rewards=100)
        UserProfile.objects.create(user=self.juror2, rewards=100)
        UserProfile.objects.create(user=self.juror3, rewards=100)

        self.task = Task.objects.create(
            title='Test Delivery',
            description='Deliver items to room 101',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client = Client()

    def test_two_sided_dispute_initiation_by_taker(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Task details were misleading.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.raised_by, self.taker)

    def test_two_sided_dispute_initiation_by_poster(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Task taker was non-responsive.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.status, 'open')
        self.assertEqual(self.task.dispute.raised_by, self.poster)

    def test_unauthorized_user_cannot_raise_dispute(self):
        self.client.login(username='stranger', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Malicious attempt.'}
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_soft_withdrawal_preserves_records(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Sample issue',
            status='open'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(dispute.status, 'withdrawn')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists(), "Dispute record must not be deleted")

    def test_reraise_dispute_after_withdrawal(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Initial issue',
            status='withdrawn'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'New issue raised by poster'}
        )

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'New issue raised by poster')

    def test_escalation_to_community_jury(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Impasse reached',
            status='open'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('escalate_to_jury', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'jury_voting')

    def test_juror_eligibility_and_vote_prevention(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Impasse reached',
            status='jury_voting'
        )

        # Poster attempting to vote should be rejected
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})
        self.assertEqual(dispute.votes.count(), 0)

        # Neutral juror can vote
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})
        self.assertEqual(dispute.votes.count(), 1)

        # Duplicate vote by same juror should be rejected
        response = self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})
        self.assertEqual(dispute.votes.count(), 1)

    def test_quorum_reach_and_escrow_settlement_favor_poster(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Impasse reached',
            status='jury_voting'
        )

        initial_poster_rewards = self.poster.userprofile.rewards

        # 2 votes favor poster, 1 vote favor taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_taker'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster.userprofile.rewards, initial_poster_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.user, self.poster)
        self.assertEqual(ledger.amount, self.task.reward)

    def test_quorum_reach_and_escrow_settlement_favor_taker(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Impasse reached',
            status='jury_voting'
        )

        initial_taker_rewards = self.taker.userprofile.rewards

        # 1 vote favor poster, 2 votes favor taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_taker'})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_poster'})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('cast_jury_vote', args=[dispute.id]), {'vote': 'favor_taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.user, self.taker)
        self.assertEqual(ledger.amount, self.task.reward)
