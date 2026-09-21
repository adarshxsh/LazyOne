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

    def test_complete_disputed_task_is_blocked(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        # Poster attempts to mark task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]), follow=True)

        self.task.refresh_from_db()
        # Task remains disputed, NOT completed
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')

        # Taker balance remains 40 (no payout)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Verify error message present in response
        messages = list(response.context['messages'])
        self.assertTrue(any("under dispute" in str(m) for m in messages))

    def test_juror_reward_and_dispute_penalty_ledger_choices(self):
        choices = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('juror_reward', choices)
        self.assertIn('dispute_penalty', choices)

    def test_dispute_resolution_juror_rewards_and_penalty(self):
        # Create 3 juror users
        juror1 = User.objects.create_user(username='juror1', password='password123')
        juror2 = User.objects.create_user(username='juror2', password='password123')
        juror3 = User.objects.create_user(username='juror3', password='password123')
        UserProfile.objects.create(user=juror1, rewards=100)
        UserProfile.objects.create(user=juror2, rewards=100)
        UserProfile.objects.create(user=juror3, rewards=100)

        # Taker raises dispute on self.task (deposit = 60, taker rewards 100 -> 40)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Resolve dispute in favor of poster (taker is losing bad actor)
        jurors = [juror1, juror2, juror3]
        dispute.resolve_dispute(winner=self.poster, losing_user=self.taker, jurors=jurors)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Check losing party (taker) has dispute_penalty entry
        penalty_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_penalty').first()
        self.assertIsNotNone(penalty_ledger)
        self.assertEqual(penalty_ledger.amount, -60)

        # Check 3 jurors got 60 // 3 = 20 points each as juror_reward
        for j in [juror1, juror2, juror3]:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 120)
            j_ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_reward').first()
            self.assertIsNotNone(j_ledger)
            self.assertEqual(j_ledger.amount, 20)

    def test_user_reward_balance_matches_ledger_sum(self):
        from django.db.models import Sum
        user = User.objects.create_user(username='test_user', password='password123')
        profile = UserProfile.objects.create(user=user, rewards=500)
        RewardLedger.objects.create(user=user, amount=500, transaction_type='initial_points', description='Initial')

        profile.rewards -= 200
        profile.save()
        task = Task.objects.create(title='Task', description='D', reward=200, posted_by=user, status='in_progress')
        RewardLedger.objects.create(user=user, task=task, amount=-200, transaction_type='task_creation', description='Created')

        profile.rewards -= 50
        profile.save()
        dispute = Dispute.objects.create(task=task, raised_by=user, deposit_amount=50, escrow_status='held')
        RewardLedger.objects.create(user=user, task=task, amount=-50, transaction_type='dispute_deposit', description='Deposit')

        dispute.forfeit_deposit()

        profile.refresh_from_db()
        ledger_sum = RewardLedger.objects.filter(user=user).aggregate(Sum('amount'))['amount__sum']
        self.assertEqual(profile.rewards, ledger_sum)

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

        # Taker rewards remain 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check penalty ledger entry for taker
        penalty_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_penalty').first()
        self.assertIsNotNone(penalty_ledger)

