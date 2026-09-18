from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum
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


class DisputeSettlementTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        # Create poster, doer, third party, and staff/arbitrator
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.arbitrator = User.objects.create_user(username='arbitrator', password='password123', is_staff=True)

        # Create user profiles with initial rewards
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer)
        self.doer_profile.rewards = 500
        self.doer_profile.save()

        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user)
        self.other_profile.rewards = 500
        self.other_profile.save()

        self.arbitrator_profile, _ = UserProfile.objects.get_or_create(user=self.arbitrator)
        self.arbitrator_profile.rewards = 500
        self.arbitrator_profile.save()

        # Record initial system points before task creation
        self.initial_system_points = sum(p.rewards for p in UserProfile.objects.all())

        # Task creation: poster creates task for 100 points
        deadline = timezone.now() + timedelta(days=1)
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('add_task'), {
            'title': 'Test Split Task',
            'description': 'Description of test task',
            'reward': '100',
            'deadline': deadline.strftime('%Y-%m-%dT%H:%M')
        })
        self.task = Task.objects.get(title='Test Split Task')

        # Doer takes task
        self.client.login(username='doer', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()

        # Doer raises dispute
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'reason': 'Incomplete work disputed'
        })
        self.dispute = Dispute.objects.get(task=self.task)

    def test_partial_settlement_fixed_points(self):
        """Test settling a dispute with custom fixed point amounts (60 doer / 40 poster)."""
        self.poster_profile.refresh_from_db()
        self.doer_profile.refresh_from_db()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '60',
            'poster_amount': '40'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.doer_profile.refresh_from_db()

        # Assert dispute and task status
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.dispute.doer_amount, 60)
        self.assertEqual(self.dispute.poster_amount, 40)
        self.assertIsNotNone(self.dispute.settled_at)
        self.assertEqual(self.dispute.resolved_by, self.poster)

        # Assert user balances
        # Poster started at 1000, paid 100 on task creation (900), received 40 refund -> 940
        self.assertEqual(self.poster_profile.rewards, 940)
        # Doer started at 500, received 60 payout -> 560
        self.assertEqual(self.doer_profile.rewards, 560)

        # Assert ledger entries
        doer_ledger = RewardLedger.objects.get(user=self.doer, task=self.task, transaction_type='dispute_settlement_payout')
        self.assertEqual(doer_ledger.amount, 60)
        poster_ledger = RewardLedger.objects.get(user=self.poster, task=self.task, transaction_type='dispute_settlement_refund')
        self.assertEqual(poster_ledger.amount, 40)

        # Assert zero-sum conservation across system (pre-creation total == post-settlement total)
        current_total_points = sum(p.rewards for p in UserProfile.objects.all())
        self.assertEqual(self.initial_system_points, current_total_points)

    def test_partial_settlement_percentage(self):
        """Test settling a dispute using percentage input (70% doer)."""
        self.client.login(username='arbitrator', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_percent': '70'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.doer_amount, 70)
        self.assertEqual(self.dispute.poster_amount, 30)
        self.assertEqual(self.dispute.resolved_by, self.arbitrator)

    def test_partial_settlement_preset_split(self):
        """Test settling a dispute using a preset split option (50/50)."""
        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'split_preset': '50_50'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.doer_amount, 50)
        self.assertEqual(self.dispute.poster_amount, 50)

    def test_settlement_validation_sum_mismatch(self):
        """Test that settlement fails if doer_amount + poster_amount != task.reward."""
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '50',
            'poster_amount': '40' # Sum is 90 != 100
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertIsNone(self.dispute.doer_amount)

    def test_settlement_validation_negative_amounts(self):
        """Test that settlement fails if negative point amounts are submitted."""
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '-10',
            'poster_amount': '110'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_unauthorized_settlement_attempt(self):
        """Test that non-participants/unauthorized users cannot settle the dispute."""
        self.client.login(username='other', password='password123')
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'split_preset': '50_50'
        })
        self.assertRedirects(response, reverse('home'))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_re_settlement_prohibited(self):
        """Test that a resolved dispute cannot be re-settled."""
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '60',
            'poster_amount': '40'
        })
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

        # Attempt second settlement
        response = self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '50',
            'poster_amount': '50'
        })
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.doer_amount, 60) # Remains unchanged
        self.assertEqual(self.dispute.poster_amount, 40)

    def test_zero_sum_conservation_ledger(self):
        """Verify zero-sum point conservation across initial creation and partial settlement ledger."""
        self.client.login(username='arbitrator', password='password123')
        self.client.post(reverse('settle_dispute', args=[self.dispute.id]), {
            'doer_amount': '75',
            'poster_amount': '25'
        })

        final_sum = sum(p.rewards for p in UserProfile.objects.all())
        self.assertEqual(self.initial_system_points, final_sum)

        # Check total net change across all ledger entries for this task
        task_ledger_sum = RewardLedger.objects.filter(task=self.task).aggregate(total=Sum('amount'))['total']
        # Task creation (-100) + doer payout (+75) + poster refund (+25) = 0
        self.assertEqual(task_ledger_sum, 0)
