from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation
from .firebase_init import update_dispute_firestore


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


class DisputeFirestoreEventsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')

        UserProfile.objects.create(user=self.poster, rewards=500)
        UserProfile.objects.create(user=self.doer, rewards=100)

        self.deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=50,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=self.deadline,
            status='in_progress'
        )

    @patch('basic.views.dispute.update_dispute_firestore')
    def test_raise_dispute_triggers_firestore_event(self, mock_update_firestore):
        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair requirement'})

        self.task.refresh_from_db()
        dispute = Dispute.objects.get(task=self.task)

        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(dispute.reason, 'Unfair requirement')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='open',
            event_type='dispute_raised',
            raised_by_username='doer'
        )

    @patch('basic.views.dispute.update_dispute_firestore')
    def test_withdraw_dispute_triggers_firestore_event(self, mock_update_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Need to withdraw', deposit_amount=50, escrow_status='held')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'in_progress')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='withdrawn',
            event_type='dispute_withdrawn',
            raised_by_username='doer'
        )

    @patch('basic.views.tasks.update_dispute_firestore')
    def test_complete_task_resolves_dispute_triggers_firestore_event(self, mock_update_firestore):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Resolving this', deposit_amount=50, escrow_status='held')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(response.status_code, 302)

        mock_update_firestore.assert_called_once_with(
            dispute_id=dispute.id,
            task_id=self.task.id,
            status='resolved',
            event_type='dispute_resolved',
            raised_by_username='doer'
        )

    @patch('firebase_admin._apps', True)
    @patch('firebase_admin.firestore.client')
    def test_update_dispute_firestore_helper_executes_set(self, mock_firestore_client):
        mock_db = MagicMock()
        mock_doc = MagicMock()
        mock_firestore_client.return_value = mock_db
        mock_db.collection.return_value.doc.return_value = mock_doc

        update_dispute_firestore(
            dispute_id=1,
            task_id=10,
            status='open',
            event_type='dispute_raised',
            raised_by_username='doer'
        )

        mock_db.collection.assert_called_once_with('disputes')
        mock_db.collection().doc.assert_called_once_with('1')
        mock_doc.set.assert_called_once()
        args, kwargs = mock_doc.set.call_args
        self.assertEqual(args[0]['dispute_id'], 1)
        self.assertEqual(args[0]['task_id'], 10)
        self.assertEqual(args[0]['status'], 'open')
        self.assertEqual(args[0]['event_type'], 'dispute_raised')
        self.assertEqual(args[0]['raised_by'], 'doer')
        self.assertTrue(kwargs.get('merge'))

    @patch('basic.firebase_init.initialize_firebase', side_effect=Exception("Firebase timeout"))
    def test_update_dispute_firestore_handles_exceptions_gracefully(self, mock_init):
        # Should catch exception and not raise error
        try:
            update_dispute_firestore(
                dispute_id=1,
                task_id=10,
                status='open',
                event_type='dispute_raised',
                raised_by_username='doer'
            )
        except Exception as e:
            self.fail(f"update_dispute_firestore raised an unexpected exception: {e}")
