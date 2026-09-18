from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


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


class DisputeModelTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_default_voting_deadline_populated(self):
        start_time = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task issue'
        )
        self.assertIsNotNone(dispute.voting_deadline)
        expected_deadline = start_time + timedelta(hours=72)
        self.assertAlmostEqual(
            dispute.voting_deadline.timestamp(),
            expected_deadline.timestamp(),
            delta=5
        )

    @override_settings(DISPUTE_VOTING_WINDOW_HOURS=24)
    def test_custom_voting_window_setting(self):
        start_time = timezone.now()
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task issue'
        )
        expected_deadline = start_time + timedelta(hours=24)
        self.assertAlmostEqual(
            dispute.voting_deadline.timestamp(),
            expected_deadline.timestamp(),
            delta=5
        )

    def test_is_expired_property(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task issue'
        )
        self.assertFalse(dispute.is_expired)

        dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()
        self.assertTrue(dispute.is_expired)

        dispute.status = 'resolved'
        dispute.save()
        self.assertFalse(dispute.is_expired)


class ResolveExpiredDisputesCommandTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Task 1: Expired dispute
        self.expired_task = Task.objects.create(
            title='Expired Task',
            description='Expired Task Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.expired_dispute = Dispute.objects.create(
            task=self.expired_task,
            raised_by=self.taker,
            reason='Issue in task',
            status='open',
            voting_deadline=timezone.now() - timedelta(hours=2)
        )

        # Task 2: Active dispute
        self.active_task = Task.objects.create(
            title='Active Task',
            description='Active Task Description',
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.active_dispute = Dispute.objects.create(
            task=self.active_task,
            raised_by=self.taker,
            reason='Another issue',
            status='open',
            voting_deadline=timezone.now() + timedelta(hours=48)
        )

    def test_resolve_expired_disputes_command(self):
        call_command('resolve_expired_disputes')

        self.expired_dispute.refresh_from_db()
        self.expired_task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.expired_dispute.status, 'resolved')
        self.assertEqual(self.expired_task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200)

        ledger = RewardLedger.objects.get(task=self.expired_task)
        self.assertEqual(ledger.user, self.poster)
        self.assertEqual(ledger.amount, 200)
        self.assertEqual(ledger.transaction_type, 'task_cancellation')

        poster_notif = Notification.objects.filter(recipient=self.poster, message__icontains='expired')
        taker_notif = Notification.objects.filter(recipient=self.taker, message__icontains='expired')
        self.assertTrue(poster_notif.exists())
        self.assertTrue(taker_notif.exists())

        self.active_dispute.refresh_from_db()
        self.active_task.refresh_from_db()
        self.assertEqual(self.active_dispute.status, 'open')
        self.assertEqual(self.active_task.status, 'disputed')


class DisputeViewsTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)
        self.task = Task.objects.create(
            title='View Test Task',
            description='View Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Testing template view rendering'
        )

    def test_dispute_detail_view(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Voting Deadline')
        self.assertContains(response, 'dispute-countdown')

    def test_my_tasks_view(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Voting Deadline')

    def test_home_view(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Voting Deadline')
