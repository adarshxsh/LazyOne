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


class StaffDisputeModerationTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Regular user
        self.user = User.objects.create_user(username='regular_user', password='password123')
        UserProfile.objects.create(user=self.user, rewards=500)

        # Staff user
        self.staff = User.objects.create_user(username='staff_mod', password='password123', is_staff=True)
        UserProfile.objects.create(user=self.staff, rewards=1000)

        # Poster & Taker
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=200)

        # Create Task
        self.task = Task.objects.create(
            title="Design Logo Task",
            description="Create a modern logo",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        # Create Dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Poster requested extra work outside scope",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

    def test_django_admin_registration(self):
        from django.contrib import admin
        self.assertIn(Dispute, admin.site._registry)
        self.assertIn(Task, admin.site._registry)
        self.assertIn(UserProfile, admin.site._registry)

    def test_non_staff_cannot_access_staff_views(self):
        self.client.login(username='regular_user', password='password123')

        # List view access
        response = self.client.get(reverse('admin_disputes'))
        self.assertEqual(response.status_code, 302)

        # Override view access
        response = self.client.get(reverse('admin_dispute_override', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 302)

    def test_staff_dispute_list_filter_and_search(self):
        self.client.login(username='staff_mod', password='password123')

        response = self.client.get(reverse('admin_disputes'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Design Logo Task")

        # Search by title query
        response = self.client.get(reverse('admin_disputes') + '?q=Design')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Design Logo Task")

        # Search non-existent
        response = self.client.get(reverse('admin_disputes') + '?q=NonExistent')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No disputes found")

        # Status filter
        response = self.client.get(reverse('admin_disputes') + '?status=open')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Design Logo Task")

        response = self.client.get(reverse('admin_disputes') + '?status=resolved')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No disputes found")

    def test_staff_moderation_override_resolve_taker(self):
        self.client.login(username='staff_mod', password='password123')

        # GET override page
        response = self.client.get(reverse('admin_dispute_override', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Design Logo Task")

        # POST resolve_taker
        response = self.client.post(
            reverse('admin_dispute_override', args=[self.dispute.id]),
            {
                'action': 'resolve_taker',
                'deposit_action': 'refund',
                'moderation_note': 'Taker submitted complete work.'
            }
        )
        self.assertRedirects(response, reverse('admin_disputes'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')

        # Taker received reward (200) + refunded deposit (50) -> 200 + 200 + 50 = 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 450)

        # Check ledger entries
        completion_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion').first()
        self.assertIsNotNone(completion_ledger)
        self.assertEqual(completion_ledger.amount, 200)

        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 50)

    def test_staff_moderation_override_resolve_poster(self):
        self.client.login(username='staff_mod', password='password123')

        response = self.client.post(
            reverse('admin_dispute_override', args=[self.dispute.id]),
            {
                'action': 'resolve_poster',
                'deposit_action': 'forfeit',
                'moderation_note': 'Taker failed to deliver.'
            }
        )
        self.assertRedirects(response, reverse('admin_disputes'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'forfeited')

        # Poster received task reward refund (200) + forfeited deposit from taker (50) -> 1000 + 200 + 50 = 1250
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1250)

        # Taker balance remains 200 (deposit was deducted when dispute was raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 200)

        # Check ledger entries
        cancellation_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(cancellation_ledger)
        self.assertEqual(cancellation_ledger.amount, 200)

    def test_invalid_moderation_action(self):
        self.client.login(username='staff_mod', password='password123')

        response = self.client.post(
            reverse('admin_dispute_override', args=[self.dispute.id]),
            {
                'action': 'invalid_action',
                'deposit_action': 'auto'
            }
        )
        self.assertRedirects(response, reverse('admin_dispute_override', args=[self.dispute.id]))

    def test_pagination_in_staff_dispute_list(self):
        self.client.login(username='staff_mod', password='password123')

        # Create 12 more disputes to test pagination (page size = 10)
        for i in range(12):
            task = Task.objects.create(
                title=f"Task {i}",
                description="Description",
                reward=100,
                posted_by=self.poster,
                taken_by=self.taker,
                status='disputed'
            )
            Dispute.objects.create(
                task=task,
                raised_by=self.poster,
                reason=f"Reason {i}",
                deposit_amount=50,
                status='open'
            )

        response = self.client.get(reverse('admin_disputes') + '?page=2')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['page_obj'].has_previous())



