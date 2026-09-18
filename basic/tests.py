from django.test import TestCase, Client, override_settings
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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminDisputeDashboardTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Regular poster user
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})

        # Regular taker user
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})

        # Non-staff user
        self.regular = User.objects.create_user(username='regular', password='password123')
        self.regular_profile, _ = UserProfile.objects.get_or_create(user=self.regular, defaults={'rewards': 500})

        # Staff user
        self.staff_user = User.objects.create_user(username='staff_mod', password='password123', is_staff=True)
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user)

        # Superuser
        self.superuser = User.objects.create_superuser(username='admin_boss', password='password123')
        self.superuser_profile, _ = UserProfile.objects.get_or_create(user=self.superuser)

        # Create tasks and disputes
        deadline = timezone.now() + timedelta(days=1)
        self.task1 = Task.objects.create(
            title='Fix Bug in Backend',
            description='Need python bug fix',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conversation1 = Conversation.objects.create(task=self.task1)
        self.conversation1.participants.add(self.poster, self.taker)

        self.dispute1 = Dispute.objects.create(
            task=self.task1,
            raised_by=self.taker,
            reason='Poster did not approve completion despite submitted work.',
            priority='high',
            status='open'
        )

        self.task2 = Task.objects.create(
            title='Design Logo',
            description='Design a clean logo',
            reward=150,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )
        self.conversation2 = Conversation.objects.create(task=self.task2)
        self.conversation2.participants.add(self.poster, self.taker)

        self.dispute2 = Dispute.objects.create(
            task=self.task2,
            raised_by=self.poster,
            reason='Taker submitted low quality logo',
            priority='low',
            status='resolved'
        )

    def test_unauthenticated_access_redirects(self):
        url = reverse('admin_dispute_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_non_staff_access_redirects(self):
        self.client.login(username='regular', password='password123')
        url = reverse('admin_dispute_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_staff_access_successful(self):
        self.client.login(username='staff_mod', password='password123')
        url = reverse('admin_dispute_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_dispute_dashboard.html')
        self.assertContains(response, 'Admin Dispute Dashboard')
        self.assertContains(response, 'Fix Bug in Backend')
        self.assertContains(response, 'Design Logo')

    def test_dashboard_filtering_by_status_and_priority(self):
        self.client.login(username='staff_mod', password='password123')

        # Filter status=open
        response = self.client.get(reverse('admin_dispute_dashboard') + '?status=open')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fix Bug in Backend')
        self.assertNotContains(response, 'Design Logo')

        # Filter priority=high
        response = self.client.get(reverse('admin_dispute_dashboard') + '?priority=high')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fix Bug in Backend')
        self.assertNotContains(response, 'Design Logo')

    def test_dashboard_search(self):
        self.client.login(username='staff_mod', password='password123')
        response = self.client.get(reverse('admin_dispute_dashboard') + '?q=Logo')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Design Logo')
        self.assertNotContains(response, 'Fix Bug in Backend')

    def test_staff_override_force_resolve_poster_wins(self):
        self.client.login(username='staff_mod', password='password123')
        initial_rewards = self.poster_profile.rewards

        override_url = reverse('admin_override_dispute', args=[self.dispute1.id])
        response = self.client.post(override_url, {
            'action': 'poster_wins',
            'notes': 'Resolved in favor of poster after reviewing work.'
        })
        self.assertRedirects(response, reverse('admin_dispute_dashboard'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_rewards + self.task1.reward)

        ledger_entry = RewardLedger.objects.filter(task=self.task1, user=self.poster).last()
        self.assertIsNotNone(ledger_entry)
        self.assertIn(str(self.staff_user.id), ledger_entry.description)

    def test_staff_override_force_resolve_taker_wins(self):
        self.client.login(username='staff_mod', password='password123')
        initial_rewards = self.taker_profile.rewards

        override_url = reverse('admin_override_dispute', args=[self.dispute1.id])
        response = self.client.post(override_url, {
            'action': 'taker_wins',
            'notes': 'Taker provided valid evidence.'
        })
        self.assertRedirects(response, reverse('admin_dispute_dashboard'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_rewards + self.task1.reward)

        ledger_entry = RewardLedger.objects.filter(task=self.task1, user=self.taker).last()
        self.assertIsNotNone(ledger_entry)
        self.assertIn(str(self.staff_user.id), ledger_entry.description)

    def test_staff_override_dismiss_dispute(self):
        self.client.login(username='staff_mod', password='password123')

        override_url = reverse('admin_override_dispute', args=[self.dispute1.id])
        response = self.client.post(override_url, {
            'action': 'dismiss',
            'notes': 'Dispute dismissed by staff.'
        })
        self.assertRedirects(response, reverse('admin_dispute_dashboard'))

        self.dispute1.refresh_from_db()
        self.task1.refresh_from_db()

        self.assertEqual(self.dispute1.status, 'resolved')
        self.assertEqual(self.task1.status, 'in_progress')

        ledger_entry = RewardLedger.objects.filter(task=self.task1).last()
        self.assertIsNotNone(ledger_entry)
        self.assertIn(str(self.staff_user.id), ledger_entry.description)
