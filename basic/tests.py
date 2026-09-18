from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation
from basic.services import ReputationService


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


class UserProfileReputationModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password123')
        self.profile = UserProfile.objects.create(user=self.user)

    def test_default_profile_reputation_fields(self):
        """Test initial default values for UserProfile reputation fields."""
        self.assertEqual(self.profile.tasks_completed_count, 0)
        self.assertEqual(self.profile.tasks_defaulted_count, 0)
        self.assertEqual(self.profile.disputes_won_count, 0)
        self.assertEqual(self.profile.disputes_lost_count, 0)
        self.assertEqual(self.profile.reputation_score, 100)
        self.assertEqual(self.profile.risk_level, 'LOW')


class ReputationServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='repuser', password='password123')
        self.profile = UserProfile.objects.create(user=self.user)

    def test_calculate_score_and_risk_level(self):
        """Test formula calculations and risk levels."""
        # Initial score
        self.assertEqual(ReputationService.calculate_reputation_score(self.profile), 100)
        self.assertEqual(ReputationService.calculate_risk_level(100, self.profile), 'LOW')

        # Add completed tasks
        self.profile.tasks_completed_count = 5
        score = ReputationService.calculate_reputation_score(self.profile)
        self.assertEqual(score, 150)
        self.assertEqual(ReputationService.calculate_risk_level(score, self.profile), 'LOW')

        # Add defaulted tasks
        self.profile.tasks_defaulted_count = 3
        score = ReputationService.calculate_reputation_score(self.profile) # 100 + 50 - 60 = 90
        self.assertEqual(score, 90)
        # 3 defaults => HIGH risk
        self.assertEqual(ReputationService.calculate_risk_level(score, self.profile), 'HIGH')

        # Floor at zero
        self.profile.tasks_defaulted_count = 10
        score = ReputationService.calculate_reputation_score(self.profile)
        self.assertEqual(score, 0)

    def test_record_task_completion_service(self):
        """Test record_task_completion increments counter and recalculates score."""
        ReputationService.record_task_completion(self.profile)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.tasks_completed_count, 1)
        self.assertEqual(self.profile.reputation_score, 110)

    def test_record_task_default_service(self):
        """Test record_task_default increments counter and recalculates score."""
        ReputationService.record_task_default(self.profile)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.tasks_defaulted_count, 1)
        self.assertEqual(self.profile.reputation_score, 80)
        self.assertEqual(self.profile.risk_level, 'MEDIUM')

    def test_record_dispute_resolution_service(self):
        """Test dispute resolution updates win/loss for involved parties."""
        winner_user = User.objects.create_user(username='winner', password='password123')
        winner_profile = UserProfile.objects.create(user=winner_user)

        loser_user = User.objects.create_user(username='loser', password='password123')
        loser_profile = UserProfile.objects.create(user=loser_user)

        ReputationService.record_dispute_resolution(winner_profile, loser_profile)

        winner_profile.refresh_from_db()
        loser_profile.refresh_from_db()

        self.assertEqual(winner_profile.disputes_won_count, 1)
        self.assertEqual(winner_profile.reputation_score, 110)

        self.assertEqual(loser_profile.disputes_lost_count, 1)
        self.assertEqual(loser_profile.reputation_score, 80)


class TaskAndDisputeLifecycleReputationTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_complete_task_view_updates_reputation(self):
        """Test completing a task increments completed count and recalculates score."""
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.tasks_completed_count, 1)
        self.assertEqual(self.taker_profile.reputation_score, 110)

    def test_abandon_task_view_updates_default_count(self):
        """Test abandoning a task increments defaulted count and recalculates score."""
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.tasks_defaulted_count, 1)
        self.assertEqual(self.taker_profile.reputation_score, 80)

    def test_resolve_dispute_view_updates_reputation(self):
        """Test resolving a dispute updates win/loss stats and risk score."""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Incomplete work')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'poster'})
        self.assertEqual(response.status_code, 302)

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.poster_profile.disputes_won_count, 1)
        self.assertEqual(self.poster_profile.reputation_score, 110)

        self.assertEqual(self.taker_profile.disputes_lost_count, 1)
        self.assertEqual(self.taker_profile.tasks_defaulted_count, 1)
        self.assertEqual(self.taker_profile.reputation_score, 60)


class ProfileViewsReputationRenderingTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.user = User.objects.create_user(username='viewuser', password='password123')
        self.profile = UserProfile.objects.create(
            user=self.user,
            tasks_completed_count=3,
            tasks_defaulted_count=0,
            disputes_won_count=1,
            disputes_lost_count=0,
            reputation_score=140,
            risk_level='LOW'
        )

        self.other_user = User.objects.create_user(username='otheruser', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user)

    def test_user_profile_view_renders_reputation_metrics(self):
        """Test user_profile_view renders reputation scores, stats, and risk badge."""
        self.client.login(username='otheruser', password='password123')
        response = self.client.get(reverse('user_profile', args=[self.user.id]))
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, '140')
        self.assertContains(response, 'Low Risk')
        self.assertContains(response, '3') # tasks_completed_count

    def test_edit_profile_view_renders_reputation_summary(self):
        """Test profile_view renders reputation summary card for logged in user."""
        self.client.login(username='viewuser', password='password123')
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'Your Reputation')
        self.assertContains(response, '140')
        self.assertContains(response, 'Low Risk')

    def test_high_risk_level_assignment(self):
        """Test assigning HIGH risk level when score drops below threshold."""
        self.profile.tasks_defaulted_count = 10
        ReputationService.update_reputation(self.profile)
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.reputation_score, 0) # Floor at 0
        self.assertEqual(self.profile.risk_level, 'HIGH')
