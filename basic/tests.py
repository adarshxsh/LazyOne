from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib import admin
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification


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


class StaffDisputeManagementTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.staff_user = User.objects.create_user(
            username='staff_admin', password='password123', is_staff=True
        )
        self.poster_user = User.objects.create_user(
            username='poster_user', password='password123'
        )
        self.worker_user = User.objects.create_user(
            username='worker_user', password='password123'
        )
        self.normal_user = User.objects.create_user(
            username='normal_user', password='password123'
        )

        # Profiles
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1000})
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster_user, defaults={'rewards': 1000})
        self.worker_profile, _ = UserProfile.objects.get_or_create(user=self.worker_user, defaults={'rewards': 500})
        self.normal_profile, _ = UserProfile.objects.get_or_create(user=self.normal_user, defaults={'rewards': 500})

        # Task and Dispute
        self.task = Task.objects.create(
            title="Clean up garden",
            description="Mow lawn and sweep leaves",
            reward=200,
            posted_by=self.poster_user,
            taken_by=self.worker_user,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker_user,
            reason="Poster refused to acknowledge completion.",
            status='open'
        )

    def test_non_staff_access_denied_to_dashboard(self):
        """Non-staff users must be redirected with an error when accessing /staff/disputes/."""
        self.client.login(username='normal_user', password='password123')
        response = self.client.get(reverse('staff_disputes'), follow=True)
        self.assertRedirects(response, reverse('home'))
        messages = list(response.context['messages'])
        self.assertTrue(any("Access denied" in str(m) or "staff" in str(m).lower() for m in messages))

    def test_non_staff_access_denied_to_resolve(self):
        """Non-staff users must be redirected when attempting to post to resolution endpoint."""
        self.client.login(username='normal_user', password='password123')
        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'reward_worker'},
            follow=True
        )
        self.assertRedirects(response, reverse('home'))

    def test_staff_dashboard_view_and_filtering(self):
        """Staff members can view dashboard, filter by status, and search."""
        self.client.login(username='staff_admin', password='password123')

        # View dashboard
        response = self.client.get(reverse('staff_disputes'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clean up garden")
        self.assertContains(response, "Staff Dispute Dashboard")

        # Filter by open status
        response = self.client.get(reverse('staff_disputes') + '?status=open')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['disputes']), 1)

        # Filter by resolved status (should be empty initially)
        response = self.client.get(reverse('staff_disputes') + '?status=resolved')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['disputes']), 0)

        # Search by username
        response = self.client.get(reverse('staff_disputes') + '?q=worker_user')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['disputes']), 1)

        # Search by non-matching query
        response = self.client.get(reverse('staff_disputes') + '?q=nonexistent')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['disputes']), 0)

    def test_staff_resolve_in_favor_of_worker(self):
        """Staff resolving in favor of worker updates worker rewards, task status, dispute status, ledger & notifications."""
        self.client.login(username='staff_admin', password='password123')
        initial_worker_rewards = self.worker_profile.rewards

        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'reward_worker'},
            follow=True
        )
        self.assertRedirects(response, reverse('staff_disputes'))

        # Refresh state
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.worker_profile.rewards, initial_worker_rewards + self.task.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.worker_user, task=self.task).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)
        self.assertEqual(ledger.transaction_type, 'task_completion')

        # Check Notifications
        notifications = Notification.objects.filter(recipient=self.worker_user)
        self.assertTrue(notifications.exists())

    def test_staff_resolve_in_favor_of_poster(self):
        """Staff refunding poster updates poster rewards, task status, dispute status, ledger & notifications."""
        self.client.login(username='staff_admin', password='password123')
        initial_poster_rewards = self.poster_profile.rewards

        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'refund_poster'},
            follow=True
        )
        self.assertRedirects(response, reverse('staff_disputes'))

        # Refresh state
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster_user, task=self.task).first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)
        self.assertEqual(ledger.transaction_type, 'task_cancellation')

    def test_staff_resolve_custom_adjustment(self):
        """Staff applying custom adjustment awards specific points to poster and worker."""
        self.client.login(username='staff_admin', password='password123')
        initial_poster_rewards = self.poster_profile.rewards
        initial_worker_rewards = self.worker_profile.rewards

        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'custom', 'poster_points': 100, 'worker_points': 100},
            follow=True
        )
        self.assertRedirects(response, reverse('staff_disputes'))

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 100)
        self.assertEqual(self.worker_profile.rewards, initial_worker_rewards + 100)

    def test_admin_models_registered(self):
        """Dispute, Task, RewardLedger, and UserProfile models must be registered in basic/admin.py."""
        self.assertTrue(admin.site.is_registered(Dispute))
        self.assertTrue(admin.site.is_registered(Task))
        self.assertTrue(admin.site.is_registered(RewardLedger))
        self.assertTrue(admin.site.is_registered(UserProfile))

    def test_navbar_displays_staff_disputes_for_staff(self):
        """Navbar in base.html displays Staff Disputes link for staff users and hides it for normal users."""
        self.client.login(username='staff_admin', password='password123')
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Staff Disputes')

        self.client.login(username='normal_user', password='password123')
        response = self.client.get(reverse('home'))
        self.assertNotContains(response, 'Staff Disputes')

    def test_unauthenticated_access_redirects(self):
        """Unauthenticated user accessing staff routes is redirected to login page."""
        response = self.client.get(reverse('staff_disputes'))
        self.assertRedirects(response, '/login/?next=/staff/disputes/')

    def test_invalid_resolution_action(self):
        """Posting invalid resolution action shows error and redirects to dispute detail."""
        self.client.login(username='staff_admin', password='password123')
        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'invalid_action'},
            follow=True
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

    def test_already_resolved_dispute(self):
        """Resolving an already resolved dispute shows warning and redirects to dispute detail."""
        self.client.login(username='staff_admin', password='password123')
        self.dispute.status = 'resolved'
        self.dispute.save()

        response = self.client.post(
            reverse('staff_resolve_dispute', args=[self.dispute.id]),
            {'action': 'reward_worker'},
            follow=True
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

