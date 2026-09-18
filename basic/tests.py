from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
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
        self.assertFalse(Dispute.objects.filter(task=self.task, status='open').exists())

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


class DisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client = Client()

    def test_poster_can_raise_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client.post(url, {'reason': 'Taker is not responsive'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.reason, 'Taker is not responsive')

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has raised a dispute', notification.message)
        self.assertEqual(notification.link, reverse('dispute_detail', args=[dispute.id]))

    def test_poster_can_withdraw_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        self.client.post(url, {'reason': 'Issue'})

        dispute = Dispute.objects.get(task=self.task)

        withdraw_url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client.post(withdraw_url)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.assertRedirects(response, reverse('my_tasks'))

        notification = Notification.objects.filter(recipient=self.taker).latest('created_at')
        self.assertIn('poster has withdrawn the dispute', notification.message)

    def test_taker_can_raise_and_withdraw_dispute(self):
        self.client.login(username='taker', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client.post(url, {'reason': 'Poster demands extra work'})

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)

        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('taker has raised a dispute', notification.message)

        withdraw_url = reverse('withdraw_dispute', args=[dispute.id])
        self.client.post(withdraw_url)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        notification2 = Notification.objects.filter(recipient=self.poster).latest('created_at')
        self.assertIn('taker has withdrawn the dispute', notification2.message)

    def test_unauthorized_user_cannot_raise_dispute(self):
        self.client.login(username='other', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client.post(url, {'reason': 'Not my business'})

        self.assertRedirects(response, reverse('my_tasks'))
        self.assertEqual(Dispute.objects.count(), 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

    def test_cannot_raise_dispute_on_available_task(self):
        available_task = Task.objects.create(
            title='Available Task',
            description='Desc',
            reward=50,
            posted_by=self.poster,
            status='available'
        )
        self.client.login(username='poster', password='password123')
        url = reverse('raise_dispute', args=[available_task.id])
        response = self.client.post(url, {'reason': 'No one took it'})

        self.assertRedirects(response, reverse('my_tasks'))
        self.assertEqual(Dispute.objects.count(), 0)

    def test_only_dispute_raiser_can_withdraw_dispute(self):
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster complaint'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='taker', password='password123')
        url = reverse('withdraw_dispute', args=[dispute.id])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 404)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

    def test_unauthorized_user_cannot_view_dispute_detail(self):
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster complaint'})

        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='other', password='password123')
        url = reverse('dispute_detail', args=[dispute.id])
        response = self.client.get(url)

        self.assertRedirects(response, reverse('home'))

    def test_my_tasks_ui_buttons_for_posted_tasks(self):
        self.client.login(username='poster', password='password123')

        # 1. In progress task should render "Raise Dispute" button
        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, "Raise Dispute")

        # 2. When poster raises dispute, "Withdraw Dispute" should be rendered
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Poster complaint'})

        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, "Withdraw Dispute")
        self.assertContains(response, "You have raised a dispute.")

        # 3. When taker raised dispute, poster sees "Mark as Complete" and "View Dispute" but not "Withdraw Dispute"
        dispute = Dispute.objects.get(task=self.task)
        dispute.raised_by = self.taker
        dispute.save()

        response = self.client.get(reverse('my_tasks'))
        self.assertContains(response, "View Dispute")
        self.assertContains(response, "This task is in dispute. You can end the dispute by completing the task.")
        self.assertNotContains(response, "Withdraw Dispute")
