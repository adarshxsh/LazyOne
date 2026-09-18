from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation, Notification


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


class DualPartyDisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        self.task = Task.objects.create(
            title='Test Task',
            description='Task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_poster_can_raise_dispute_and_notifies_taker(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Taker stopped communicating'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Taker stopped communicating')
        
        # Check notification sent to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has raised a dispute', notification.message)
        self.assertIn(self.task.title, notification.message)
        self.assertEqual(notification.link, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_taker_can_raise_dispute_and_notifies_poster(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker.post(url, {'reason': 'Poster demands extra work'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        
        # Check notification sent to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('taker has raised a dispute', notification.message)
        self.assertIn(self.task.title, notification.message)

    def test_non_participant_cannot_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_other.post(url, {'reason': 'Unrelated user dispute'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_on_available_task(self):
        available_task = Task.objects.create(
            title='Available Task',
            description='Desc',
            reward=50,
            posted_by=self.poster,
            status='available'
        )
        url = reverse('raise_dispute', args=[available_task.id])
        self.client_poster.post(url, {'reason': 'Dispute available task'})
        
        available_task.refresh_from_db()
        self.assertEqual(available_task.status, 'available')
        self.assertFalse(hasattr(available_task, 'dispute'))

    def test_only_creator_can_withdraw_dispute(self):
        # Poster raises dispute
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Poster dispute',
            deposit_amount=50,
            escrow_status='held'
        )
        self.task.status = 'disputed'
        self.task.save()

        withdraw_url = reverse('withdraw_dispute', args=[dispute.id])

        # Taker tries to withdraw poster's dispute -> blocked
        response_taker = self.client_taker.post(withdraw_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Poster withdraws poster's dispute -> success
        response_poster = self.client_poster.post(withdraw_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')

        # Notification sent to taker upon poster withdrawal
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has withdrawn the dispute', notification.message)

    def test_poster_can_mark_disputed_task_as_complete(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        complete_url = reverse('complete_task', args=[self.task.id])
        self.client_poster.get(complete_url)

        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        
        taker_profile = UserProfile.objects.get(user=self.taker)
        self.assertEqual(taker_profile.rewards, 1600)  # 1500 + 100

    def test_my_tasks_ui_rendering_permissions(self):
        # In progress task: poster should see Raise Dispute button modal trigger
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, f"openDisputeModal('{self.task.id}'")

        # Now raise dispute by poster
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        # Poster views my_tasks -> sees Withdraw Dispute for poster-raised dispute
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, reverse('withdraw_dispute', args=[dispute.id]))

        # Taker views my_tasks -> does NOT see Withdraw Dispute for poster-raised dispute
        response_taker = self.client_taker.get(reverse('my_tasks'))
        self.assertNotContains(response_taker, f'action="{reverse("withdraw_dispute", args=[dispute.id])}"')
