from django.test import TestCase, Client, override_settings
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReputationSystemTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)

    def test_user_profile_reputation_fields_defaults_and_properties(self):
        """Verify UserProfile schema contains reputation fields with appropriate defaults and helper properties."""
        profile = self.poster_profile
        self.assertEqual(profile.trust_score, 100)
        self.assertEqual(profile.completed_tasks, 0)
        self.assertEqual(profile.abandoned_tasks, 0)
        self.assertEqual(profile.dispute_wins, 0)
        self.assertEqual(profile.dispute_losses, 0)

        self.assertEqual(profile.reputation_score, 100)
        self.assertEqual(profile.completion_percentage, 100.0)
        self.assertEqual(profile.risk_tier, 'Standard')
        self.assertEqual(profile.trust_badge, 'Standard')

        profile.trust_score = 130
        self.assertEqual(profile.risk_tier, 'Trusted')
        self.assertEqual(profile.trust_badge, 'Low Risk')

        profile.trust_score = 50
        self.assertEqual(profile.risk_tier, 'High Risk')
        self.assertEqual(profile.trust_badge, 'High Risk')

    def test_task_completion_increments_completed_tasks_and_trust_score(self):
        """Completing a task automatically increments completed_tasks and adds reputation points to the taker."""
        task = Task.objects.create(
            title='Test Task', description='Desc', reward=100,
            posted_by=self.poster, taken_by=self.taker, status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)

        self.taker_profile.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertEqual(self.taker_profile.completed_tasks, 1)
        self.assertEqual(self.taker_profile.trust_score, 110)

    def test_task_abandonment_increments_abandoned_tasks_and_deducts_trust_score(self):
        """Abandoning a task automatically increments abandoned_tasks and deducts trust score with floor 0."""
        task = Task.objects.create(
            title='Test Task', description='Desc', reward=100,
            posted_by=self.poster, taken_by=self.taker, status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('abandon_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)

        self.taker_profile.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertEqual(self.taker_profile.abandoned_tasks, 1)
        self.assertEqual(self.taker_profile.trust_score, 80)

        # Verify minimum floor bound of 0
        self.taker_profile.trust_score = 10
        self.taker_profile.save()
        task.taken_by = self.taker
        task.status = 'in_progress'
        task.save()

        self.client.get(reverse('abandon_task', args=[task.id]))
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.trust_score, 0)

    def test_dispute_resolution_updates_participant_reputation(self):
        """Resolving a dispute updates dispute_wins, dispute_losses, and trust scores for participants."""
        task = Task.objects.create(
            title='Disputed Task', description='Desc', reward=100,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.poster, reason='Unfinished work')

        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {'winner': 'taker'})
        self.assertEqual(response.status_code, 302)

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.taker_profile.dispute_wins, 1)
        self.assertEqual(self.taker_profile.trust_score, 115)
        self.assertEqual(self.poster_profile.dispute_losses, 1)
        self.assertEqual(self.poster_profile.trust_score, 85)

    def test_profile_views_display_reputation_metrics(self):
        """User profile views present trust badge, completion percentage, and dispute statistics."""
        self.taker_profile.trust_score = 125
        self.taker_profile.completed_tasks = 8
        self.taker_profile.abandoned_tasks = 2
        self.taker_profile.dispute_wins = 3
        self.taker_profile.dispute_losses = 1
        self.taker_profile.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('user_profile', args=[self.taker.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Trust Score')
        self.assertContains(response, '125')
        self.assertContains(response, '80.0%')  # Completion percentage (8 / 10)
        self.assertContains(response, '8 / 2')   # Completed / Abandoned
        self.assertContains(response, '3 / 1')   # Dispute W/L

    def test_high_risk_or_low_reputation_blocked_from_claiming_task(self):
        """Users with trust score lower than task.min_trust_score are blocked from taking tasks."""
        task = Task.objects.create(
            title='High Requirement Task', description='Desc', reward=100,
            posted_by=self.poster, status='available', min_trust_score=110,
            deadline=timezone.now() + timedelta(days=1)
        )
        self.taker_profile.trust_score = 90
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('take_task', args=[task.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any("Task assignment denied" in str(m) for m in messages_list))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
