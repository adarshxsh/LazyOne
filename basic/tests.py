from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification
from basic.admin import (
    UserProfileAdmin, TaskAdmin, RewardLedgerAdmin, DisputeAdmin,
    force_resolve_refund_poster, force_resolve_award_taker, freeze_dispute
)


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


class AdminRegistrationTest(TestCase):
    def test_models_registered_in_admin(self):
        self.assertTrue(admin.site.is_registered(UserProfile))
        self.assertTrue(admin.site.is_registered(Task))
        self.assertTrue(admin.site.is_registered(RewardLedger))
        self.assertTrue(admin.site.is_registered(Dispute))

    def test_dispute_admin_configuration(self):
        dispute_admin = admin.site._registry[Dispute]
        self.assertIn('status', dispute_admin.list_filter)
        self.assertIn('created_at', dispute_admin.list_filter)
        self.assertIn('task__title', dispute_admin.search_fields)
        self.assertIn('raised_by__username', dispute_admin.search_fields)


class DisputeModerationTest(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff, defaults={'rewards': 1500})

        self.task = Task.objects.create(
            title='Test Task for Dispute',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task was incomplete according to poster',
            status='open'
        )

        self.client = Client()

    def test_admin_action_force_resolve_refund_poster(self):
        dispute_admin = DisputeAdmin(Dispute, admin.site)
        queryset = Dispute.objects.filter(id=self.dispute.id)

        class DummyRequest:
            pass

        request = DummyRequest()
        messages_list = []

        def dummy_message_user(req, msg, level=None):
            messages_list.append(msg)

        dispute_admin.message_user = dummy_message_user

        force_resolve_refund_poster(dispute_admin, request, queryset)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)
        self.assertEqual(ledger.transaction_type, 'task_cancellation')

        poster_notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(poster_notif)

    def test_admin_action_force_resolve_award_taker(self):
        dispute_admin = DisputeAdmin(Dispute, admin.site)
        queryset = Dispute.objects.filter(id=self.dispute.id)

        class DummyRequest:
            pass

        request = DummyRequest()
        dispute_admin.message_user = lambda req, msg, level=None: None

        force_resolve_award_taker(dispute_admin, request, queryset)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 700)

        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)
        self.assertEqual(ledger.transaction_type, 'task_completion')

    def test_admin_action_freeze_dispute(self):
        dispute_admin = DisputeAdmin(Dispute, admin.site)
        queryset = Dispute.objects.filter(id=self.dispute.id)

        class DummyRequest:
            pass

        request = DummyRequest()
        dispute_admin.message_user = lambda req, msg, level=None: None

        freeze_dispute(dispute_admin, request, queryset)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_dispute_detail_staff_view_and_moderation_panel(self):
        self.client.login(username='staff', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Staff Moderation Controls')
        self.assertContains(response, 'Force-resolve: Refund Poster')
        self.assertContains(response, 'Force-resolve: Award Taker')

    def test_staff_resolve_dispute_refund_poster_via_post(self):
        self.client.login(username='staff', password='password123')
        url = reverse('staff_resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'action': 'refund_poster', 'note': 'Valid cancellation complaint'})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1200)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.transaction_type, 'task_cancellation')

        notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIn('Valid cancellation complaint', notif.message)

    def test_staff_resolve_dispute_award_taker_via_post(self):
        self.client.login(username='staff', password='password123')
        url = reverse('staff_resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'action': 'award_taker', 'note': 'Work verified by staff'})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 700)

    def test_non_staff_user_cannot_execute_staff_resolve_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('staff_resolve_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'action': 'refund_poster'})

        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
