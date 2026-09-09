from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, RewardLedger
from django.utils import timezone
from django.urls import reverse
from datetime import timedelta
from django.db.models import Sum


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
