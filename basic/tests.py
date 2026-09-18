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

        # Record is deleted on withdrawal
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

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


class TaskDisputeValidationTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password')
        self.taker1 = User.objects.create_user(username='taker1', password='password')
        self.taker2 = User.objects.create_user(username='taker2', password='password')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker1_profile, _ = UserProfile.objects.get_or_create(user=self.taker1, defaults={'rewards': 1000})
        self.taker2_profile, _ = UserProfile.objects.get_or_create(user=self.taker2, defaults={'rewards': 1000})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password')

        self.client_taker1 = Client()
        self.client_taker1.login(username='taker1', password='password')

        self.client_taker2 = Client()
        self.client_taker2.login(username='taker2', password='password')

    def _create_task(self, **kwargs):
        task = Task.objects.create(**kwargs)
        if task.taken_by:
            conv, _ = Conversation.objects.get_or_create(task=task)
            conv.participants.add(task.posted_by, task.taken_by)
        return task

    def test_accept_cancellation_rejects_if_not_in_progress(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='disputed',
            cancellation_requested=True,
            deadline=timezone.now() + timedelta(days=1)
        )

        response = self.client_taker1.get(reverse('accept_cancellation', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertEqual(task.taken_by, self.taker1)

    def test_accept_cancellation_deletes_dispute_and_resets_available(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            cancellation_requested=True,
            deadline=timezone.now() + timedelta(days=1)
        )
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue")

        response = self.client_taker1.get(reverse('accept_cancellation', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(task.cancellation_requested)
        self.assertFalse(Dispute.objects.filter(task=task).exists())

    def test_abandon_task_deletes_dispute_and_resets_available(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue")

        response = self.client_taker1.get(reverse('abandon_task', args=[task.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)
        self.assertFalse(Dispute.objects.filter(task=task).exists())

    def test_withdraw_dispute_rejects_if_not_open_or_not_disputed(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='completed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker1, reason="Issue", status='resolved')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

    def test_withdraw_dispute_succeeds_when_open_and_disputed(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(
            task=task,
            raised_by=self.taker1,
            reason="Issue",
            status='open',
            deposit_amount=50,
            escrow_status='held'
        )

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_new_taker_can_raise_dispute_on_reopened_task(self):
        task = self._create_task(
            title="Test Task",
            description="Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        # Taker 1 abandons task with dispute
        Dispute.objects.create(task=task, raised_by=self.taker1, reason="Old issue")
        self.client_taker1.get(reverse('abandon_task', args=[task.id]))

        task.refresh_from_db()
        self.assertEqual(task.status, 'available')

        # Taker 2 takes task
        self.client_taker2.get(reverse('take_task', args=[task.id]))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker2)

        # Taker 2 raises dispute
        response = self.client_taker2.post(
            reverse('raise_dispute', args=[task.id]),
            {'reason': 'New issue for taker 2'}
        )
        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(task=task, raised_by=self.taker2).exists())
        self.assertEqual(task.dispute.reason, 'New issue for taker 2')
