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


class StaffDisputeDashboardTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.staff_user = User.objects.create_user(username='staffmember', password='password123', is_staff=True)
        self.non_staff = User.objects.create_user(username='regularuser', password='password123', is_staff=False)
        UserProfile.objects.create(user=self.non_staff, rewards=500)

        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.worker = User.objects.create_user(username='worker_user', password='password123')
        self.worker_profile = UserProfile.objects.create(user=self.worker, rewards=200)

        # Task and Conversation
        self.task = Task.objects.create(
            title="Design Logo Task",
            description="Create a modern company logo.",
            reward=400,
            posted_by=self.poster,
            taken_by=self.worker,
            status='disputed'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.worker)

        from .models import Message, Notification
        self.msg1 = Message.objects.create(conversation=self.conversation, sender=self.worker, content="I completed the logo design.")
        self.msg2 = Message.objects.create(conversation=self.conversation, sender=self.poster, content="This is not what I asked for.")

        # Dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason="Poster refuses to accept completed work.",
            deposit_amount=80,
            escrow_status='held',
            status='open'
        )

        # Ledger
        RewardLedger.objects.create(
            user=self.worker,
            task=self.task,
            amount=-80,
            transaction_type='dispute_deposit',
            description="Deposit bond held"
        )

    def test_staff_authorization_enforced(self):
        url_index = reverse('admin_dispute_list')
        url_detail = reverse('admin_dispute_detail', args=[self.dispute.id])
        url_resolve = reverse('admin_dispute_resolve', args=[self.dispute.id])

        # Unauthenticated user
        res = self.client.get(url_index)
        self.assertIn(res.status_code, [302, 403])

        # Non-staff user
        self.client.login(username='regularuser', password='password123')
        res_index = self.client.get(url_index)
        self.assertIn(res_index.status_code, [302, 403])

        res_detail = self.client.get(url_detail)
        self.assertIn(res_detail.status_code, [302, 403])

        res_resolve = self.client.post(url_resolve, {'resolution_action': 'favor_worker'})
        self.assertIn(res_resolve.status_code, [302, 403])

        # Staff user
        self.client.login(username='staffmember', password='password123')
        res_staff_index = self.client.get(url_index)
        self.assertEqual(res_staff_index.status_code, 200)

        res_staff_detail = self.client.get(url_detail)
        self.assertEqual(res_staff_detail.status_code, 200)

    def test_dispute_index_filtering_and_search(self):
        self.client.login(username='staffmember', password='password123')

        # Status filter
        res_open = self.client.get(reverse('admin_dispute_list') + '?status=open')
        self.assertEqual(res_open.status_code, 200)
        self.assertContains(res_open, "Design Logo Task")

        res_resolved = self.client.get(reverse('admin_dispute_list') + '?status=resolved')
        self.assertEqual(res_resolved.status_code, 200)
        self.assertNotContains(res_resolved, "Design Logo Task")

        # Escrow filter
        res_held = self.client.get(reverse('admin_dispute_list') + '?escrow_status=held')
        self.assertEqual(res_held.status_code, 200)
        self.assertContains(res_held, "Design Logo Task")

        # Keyword search
        res_search_hit = self.client.get(reverse('admin_dispute_list') + '?q=Logo')
        self.assertEqual(res_search_hit.status_code, 200)
        self.assertContains(res_search_hit, "Design Logo Task")

        res_search_miss = self.client.get(reverse('admin_dispute_list') + '?q=NonExistentKeyword123')
        self.assertEqual(res_search_miss.status_code, 200)
        self.assertNotContains(res_search_miss, "Design Logo Task")

    def test_dispute_detail_view_renders_transcript_and_ledger(self):
        self.client.login(username='staffmember', password='password123')
        res = self.client.get(reverse('admin_dispute_detail', args=[self.dispute.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Design Logo Task")
        self.assertContains(res, "I completed the logo design.")
        self.assertContains(res, "This is not what I asked for.")
        self.assertContains(res, "Deposit bond held")

    def test_resolve_dispute_favor_worker(self):
        self.client.login(username='staffmember', password='password123')
        res = self.client.post(
            reverse('admin_dispute_resolve', args=[self.dispute.id]),
            {'resolution_action': 'favor_worker', 'admin_note': 'Worker delivered per spec.'}
        )
        self.assertRedirects(res, reverse('admin_dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'completed')
        # Worker balance: 200 + 400 (reward) + 80 (deposit refund) = 680
        self.assertEqual(self.worker_profile.rewards, 680)

        # Check notifications
        from .models import Notification
        self.assertTrue(Notification.objects.filter(recipient=self.worker).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_resolve_dispute_favor_poster(self):
        self.client.login(username='staffmember', password='password123')
        res = self.client.post(
            reverse('admin_dispute_resolve', args=[self.dispute.id]),
            {'resolution_action': 'favor_poster', 'admin_note': 'Work incomplete.'}
        )
        self.assertRedirects(res, reverse('admin_dispute_detail', args=[self.dispute.id]))

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.escrow_status, 'forfeited')
        self.assertEqual(self.task.status, 'cancelled')
        # Poster balance: 1000 + 400 (task refund) + 80 (forfeited bond) = 1480
        self.assertEqual(self.poster_profile.rewards, 1480)

    def test_django_admin_registration(self):
        from django.contrib import admin
        from .models import Dispute, Task, RewardLedger, UserProfile
        self.assertIn(Dispute, admin.site._registry)
        self.assertIn(Task, admin.site._registry)
        self.assertIn(RewardLedger, admin.site._registry)
        self.assertIn(UserProfile, admin.site._registry)


