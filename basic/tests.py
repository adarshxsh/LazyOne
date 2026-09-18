from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib import admin

from .models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation


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


class StaffDisputeDashboardTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create users
        self.staff_user = User.objects.create_user(
            username='staff_mod', password='password123', is_staff=True
        )
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.regular_user = User.objects.create_user(username='regular_user', password='password123')
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, rewards=1000)

        # Create tasks & disputes
        deadline = timezone.now() + timedelta(days=1)
        self.task1 = Task.objects.create(
            title="Clean Room 101",
            description="Thorough cleaning required",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conv1 = Conversation.objects.create(task=self.task1)
        self.conv1.participants.add(self.poster, self.taker)

        self.dispute1 = Dispute.objects.create(
            task=self.task1,
            raised_by=self.taker,
            reason="Poster claims room was not cleaned properly",
            status='open'
        )

        self.task2 = Task.objects.create(
            title="Deliver Notes",
            description="Hand over lecture notes",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conv2 = Conversation.objects.create(task=self.task2)
        self.conv2.participants.add(self.poster, self.taker)

        self.dispute2 = Dispute.objects.create(
            task=self.task2,
            raised_by=self.taker,
            reason="Notes were delivered late",
            status='resolved'
        )

    def test_non_staff_access_dashboard_denied(self):
        self.client.login(username='regular_user', password='password123')
        response = self.client.get(reverse('staff_dispute_manage'), follow=True)
        self.assertRedirects(response, reverse('home'))
        messages = [m.message for m in response.context['messages']]
        self.assertTrue(any('not authorized' in m.lower() for m in messages))

    def test_unauthenticated_access_dashboard_redirects_login(self):
        response = self.client.get(reverse('staff_dispute_manage'))
        self.assertRedirects(response, '/login/?next=/disputes/manage/')

    def test_staff_access_dashboard_success(self):
        self.client.login(username='staff_mod', password='password123')
        response = self.client.get(reverse('staff_dispute_manage'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'disputes_manage.html')
        self.assertIn('disputes', response.context)

    def test_dashboard_filtering_by_status(self):
        self.client.login(username='staff_mod', password='password123')

        # Open disputes
        response_open = self.client.get(reverse('staff_dispute_manage') + '?status=open')
        disputes_open = list(response_open.context['disputes'])
        self.assertEqual(len(disputes_open), 1)
        self.assertEqual(disputes_open[0].id, self.dispute1.id)

        # Resolved disputes
        response_resolved = self.client.get(reverse('staff_dispute_manage') + '?status=resolved')
        disputes_resolved = list(response_resolved.context['disputes'])
        self.assertEqual(len(disputes_resolved), 1)
        self.assertEqual(disputes_resolved[0].id, self.dispute2.id)

        # All disputes
        response_all = self.client.get(reverse('staff_dispute_manage') + '?status=all')
        disputes_all = list(response_all.context['disputes'])
        self.assertEqual(len(disputes_all), 2)

    def test_dashboard_search_functionality(self):
        self.client.login(username='staff_mod', password='password123')

        # Search by task title
        response = self.client.get(reverse('staff_dispute_manage') + '?search=Clean')
        disputes = list(response.context['disputes'])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0].id, self.dispute1.id)

        # Search by username
        response_user = self.client.get(reverse('staff_dispute_manage') + '?search=poster_user')
        disputes_user = list(response_user.context['disputes'])
        self.assertEqual(len(disputes_user), 2)

    def test_non_staff_cannot_resolve_dispute(self):
        self.client.login(username='regular_user', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])
        response = self.client.post(url, {'action': 'award_taker'}, follow=True)
        self.assertRedirects(response, reverse('home'))

        self.dispute1.refresh_from_db()
        self.assertEqual(self.dispute1.status, 'open')

    def test_resolve_dispute_award_taker(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        initial_taker_rewards = self.taker_profile.rewards
        response = self.client.post(url, {'action': 'award_taker', 'note': 'Well done'}, follow=True)
        
        self.assertRedirects(response, reverse('staff_dispute_manage'))

        # Check DB states
        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task1.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task1, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task1.reward)

        # Check Notifications
        notifications_poster = Notification.objects.filter(recipient=self.poster)
        notifications_taker = Notification.objects.filter(recipient=self.taker)
        self.assertTrue(notifications_poster.exists())
        self.assertTrue(notifications_taker.exists())

    def test_resolve_dispute_refund_poster(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        initial_poster_rewards = self.poster_profile.rewards
        response = self.client.post(url, {'action': 'refund_poster', 'note': 'Incomplete work'}, follow=True)

        self.assertRedirects(response, reverse('staff_dispute_manage'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task1.reward)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task1, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task1.reward)

        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_resolve_dispute_dismiss(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('resolve_dispute', args=[self.dispute1.id])

        response = self.client.post(url, {'action': 'dismiss'}, follow=True)

        self.assertRedirects(response, reverse('staff_dispute_manage'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'in_progress')

        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_admin_model_registrations(self):
        self.assertTrue(admin.site.is_registered(UserProfile))
        self.assertTrue(admin.site.is_registered(Task))
        self.assertTrue(admin.site.is_registered(RewardLedger))
        self.assertTrue(admin.site.is_registered(Dispute))
