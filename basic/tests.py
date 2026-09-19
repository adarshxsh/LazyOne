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


class ReputationAndRiskTierTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.user1 = User.objects.create_user(username='user1', password='password123')
        self.profile1 = UserProfile.objects.create(user=self.user1, rewards=1500)

        self.user2 = User.objects.create_user(username='user2', password='password123')
        self.profile2 = UserProfile.objects.create(user=self.user2, rewards=1500)

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title="Reputation Task",
            description="Task for testing reputation",
            reward=200,
            posted_by=self.user1,
            taken_by=self.user2,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_userprofile_reputation_defaults(self):
        self.assertEqual(self.profile1.reputation_score, 100)
        self.assertEqual(self.profile1.tasks_completed, 0)
        self.assertEqual(self.profile1.tasks_posted, 0)
        self.assertEqual(self.profile1.tasks_cancelled, 0)
        self.assertEqual(self.profile1.disputes_won, 0)
        self.assertEqual(self.profile1.disputes_lost, 0)
        self.assertEqual(self.profile1.risk_tier, 'LOW')
        self.assertEqual(self.profile1.completion_rate, 100.0)

    def test_add_task_increments_tasks_posted(self):
        self.client.login(username='user1', password='password123')
        deadline_str = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(reverse('add_task'), {
            'title': 'New Posted Task',
            'description': 'Description',
            'reward': '100',
            'deadline': deadline_str
        })
        self.assertRedirects(response, reverse('home'))
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.tasks_posted, 1)

    def test_complete_task_updates_reputation_and_counters(self):
        self.client.login(username='user1', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.profile2.refresh_from_db()
        self.assertEqual(self.profile2.tasks_completed, 1)
        self.assertEqual(self.profile2.reputation_score, 110)
        self.assertEqual(self.profile2.risk_tier, 'LOW')

    def test_abandon_task_penalizes_reputation_and_increments_cancelled(self):
        self.client.login(username='user2', password='password123')
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))
        self.profile2.refresh_from_db()
        self.assertEqual(self.profile2.tasks_cancelled, 1)
        self.assertEqual(self.profile2.reputation_score, 80)

    def test_reputation_score_floor_at_zero(self):
        self.profile1.update_reputation(-500)
        self.assertEqual(self.profile1.reputation_score, 0)

    def test_risk_tier_classification_rules(self):
        # Initial: LOW
        self.assertEqual(self.profile1.risk_tier, 'LOW')

        # Medium risk when reputation score drops below 80 or 1 dispute lost or cancellations > 0
        self.profile1.update_reputation(-30) # score becomes 70
        self.assertEqual(self.profile1.risk_tier, 'MEDIUM')

        # High risk when score < 50 or lost disputes >= 2
        self.profile1.disputes_lost = 2
        self.profile1.update_reputation(-30) # score becomes 40
        self.assertEqual(self.profile1.risk_tier, 'HIGH')

    def test_dispute_resolution_updates_won_lost_counters_and_reputation(self):
        # user2 raises dispute
        self.client.login(username='user2', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task dispute'})

        dispute = Dispute.objects.get(task=self.task)

        # Staff/poster resolves in favor of taker (user2)
        self.client.login(username='user1', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        self.profile2.refresh_from_db()
        self.profile1.refresh_from_db()

        self.assertEqual(self.profile2.disputes_won, 1)
        self.assertGreater(self.profile2.reputation_score, 100)
        self.assertEqual(self.profile1.disputes_lost, 1)
        self.assertLess(self.profile1.reputation_score, 100)

    def test_risk_tier_adaptive_collateral(self):
        base_bond = self.task.deposit_bond_amount # 40 (20% of 200) -> min 50 applies -> 50

        self.profile2.risk_tier = 'LOW'
        self.profile2.save()
        self.assertEqual(self.task.deposit_bond_amount_for_user(self.user2), base_bond)

        self.profile2.risk_tier = 'MEDIUM'
        self.profile2.save()
        self.assertEqual(self.task.deposit_bond_amount_for_user(self.user2), int(base_bond * 1.5))

        self.profile2.risk_tier = 'HIGH'
        self.profile2.save()
        self.assertEqual(self.task.deposit_bond_amount_for_user(self.user2), int(base_bond * 2.0))

    def test_profile_views_render_reputation_metrics(self):
        self.client.login(username='user1', password='password123')
        # View own profile
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reputation & Trust Profile')
        self.assertContains(response, 'Low Risk')

        # View user2 profile
        response = self.client.get(reverse('user_profile', args=[self.user2.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Trust & Reputation Metrics')
        self.assertContains(response, 'Low Risk')

    def test_profile_update_form_cannot_tamper_reputation(self):
        self.profile1.reputation_score = 30
        self.profile1.risk_tier = 'HIGH'
        self.profile1.save()

        self.client.login(username='user1', password='password123')
        # Post form trying to manipulate reputation and risk_tier
        self.client.post(reverse('profile'), {
            'first_name': 'Hacker',
            'reputation_score': '9999',
            'risk_tier': 'LOW',
            'disputes_lost': '0'
        })

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.reputation_score, 30)
        self.assertEqual(self.profile1.risk_tier, 'HIGH')
        self.assertEqual(self.profile1.first_name, 'Hacker')


