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


class SymmetricalFixedBondStakingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster2', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker2', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.task = Task.objects.create(
            title="Symmetrical Test Task",
            description="Description",
            reward=300, # Bond = 60
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_poster_can_submit_counter_deposit(self):
        # Taker raises dispute
        self.client.login(username='taker2', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work rejected'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertFalse(dispute.has_counter_deposit)
        self.assertEqual(dispute.worker_deposit_amount, 60)
        self.assertEqual(dispute.worker_escrow_status, 'held')
        self.assertEqual(dispute.poster_escrow_status, 'pending')

        # Poster submits counter-deposit
        self.client.login(username='poster2', password='password123')
        response = self.client.post(reverse('post_counter_deposit', args=[dispute.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertTrue(dispute.has_counter_deposit)
        self.assertEqual(dispute.poster_deposit_amount, 60)
        self.assertEqual(dispute.poster_escrow_status, 'held')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 940) # 1000 - 60

        # Check ledger entry
        ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_counter_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_non_response_counter_party_auto_resolves_after_48h(self):
        from django.core.management import call_command

        # Taker raises dispute
        self.client.login(username='taker2', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unresponsive poster'})

        dispute = Dispute.objects.get(task=self.task)
        # Fast-forward 49 hours
        Dispute.objects.filter(id=dispute.id).update(
            created_at=timezone.now() - timedelta(hours=49),
            counter_bond_deadline=timezone.now() - timedelta(hours=1)
        )

        call_command('resolve_expired_disputes')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.taker_profile.refresh_from_db()
        # Initial 500 - 60 (bond) + 60 (refund) + 300 (reward) = 800
        self.assertEqual(self.taker_profile.rewards, 800)

    def test_juror_staking_voting_and_slash_pool_redistribution(self):
        # 1. Worker raises dispute & poster counter-deposits
        self.client.login(username='taker2', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster2', password='password123')
        self.client.post(reverse('post_counter_deposit', args=[dispute.id]))

        # Create 4 independent jurors
        jurors = []
        for i in range(4):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            p = UserProfile.objects.create(user=u, rewards=100)
            jurors.append((u, p))

        # Jurors 0, 1, 2 vote for Poster; Juror 3 votes for Taker
        for i in range(3):
            self.client.login(username=f'juror_{i}', password='password123')
            response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'choice': 'poster'})
            self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.client.login(username='juror_3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'choice': 'taker'})

        self.assertEqual(dispute.votes.count(), 4)

        # Check juror stake deducted (100 -> 80)
        for u, p in jurors:
            p.refresh_from_db()
            self.assertEqual(p.rewards, 80)

        # 2. Resolve dispute
        from basic.views.dispute import resolve_dispute_instance
        resolve_dispute_instance(dispute)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Minority juror_3 slashed (rewards remain 80), ledger has juror_slash
        j3_user, j3_prof = jurors[3]
        j3_prof.refresh_from_db()
        self.assertEqual(j3_prof.rewards, 80)
        self.assertTrue(RewardLedger.objects.filter(user=j3_user, transaction_type='juror_slash').exists())

        # Majority jurors (0, 1, 2) get 20 (stake) + floor(20 / 3) = 6 -> 26 pts returned (80 + 26 = 106)
        for i in range(3):
            ju, jp = jurors[i]
            jp.refresh_from_db()
            self.assertEqual(jp.rewards, 106)
            self.assertTrue(RewardLedger.objects.filter(user=ju, transaction_type='juror_reward').exists())

        # Check all transaction types logged
        self.assertTrue(RewardLedger.objects.filter(transaction_type='dispute_counter_deposit').exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='juror_stake').exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='juror_reward').exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='juror_slash').exists())

    def test_juror_voting_cap_at_11(self):
        self.client.login(username='taker2', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster2', password='password123')
        self.client.post(reverse('post_counter_deposit', args=[dispute.id]))

        # Create 12 jurors
        for i in range(12):
            u = User.objects.create_user(username=f'cap_juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)

        for i in range(11):
            self.client.login(username=f'cap_juror_{i}', password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'choice': 'poster'})

        # 12th juror attempt
        j12 = User.objects.get(username='cap_juror_11')
        self.assertFalse(dispute.can_vote(j12))


