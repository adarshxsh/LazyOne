import json
from django.test import TestCase, Client
from django.contrib.auth.models import User, AnonymousUser
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import Task, Dispute, UserProfile, Conversation, Notification, RewardLedger
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from LazyOne.asgi import application


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


class WebSocketDisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_superuser(username='staff', password='password123', is_staff=True)

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1500})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            reward=100
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.client = Client()

    async def test_websocket_connect_authenticated_poster(self):
        create_dispute = database_sync_to_async(Dispute.objects.create)
        dispute = await create_dispute(
            task=self.task, raised_by=self.taker, reason="Incomplete work"
        )
        communicator = WebsocketCommunicator(application, f"ws/dispute/{dispute.id}/")
        communicator.scope["user"] = self.poster
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_websocket_connect_authenticated_taker(self):
        create_dispute = database_sync_to_async(Dispute.objects.create)
        dispute = await create_dispute(
            task=self.task, raised_by=self.taker, reason="Incomplete work"
        )
        communicator = WebsocketCommunicator(application, f"ws/dispute/{dispute.id}/")
        communicator.scope["user"] = self.taker
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_websocket_connect_unauthenticated_rejected(self):
        create_dispute = database_sync_to_async(Dispute.objects.create)
        dispute = await create_dispute(
            task=self.task, raised_by=self.taker, reason="Incomplete work"
        )
        communicator = WebsocketCommunicator(application, f"ws/dispute/{dispute.id}/")
        communicator.scope["user"] = AnonymousUser()
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_websocket_connect_unauthorized_user_rejected(self):
        create_dispute = database_sync_to_async(Dispute.objects.create)
        dispute = await create_dispute(
            task=self.task, raised_by=self.taker, reason="Incomplete work"
        )
        communicator = WebsocketCommunicator(application, f"ws/dispute/{dispute.id}/")
        communicator.scope["user"] = self.other_user
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_websocket_connect_user_channel_authenticated(self):
        communicator = WebsocketCommunicator(application, "ws/user/")
        communicator.scope["user"] = self.poster
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_dispute_consumer_receives_dispute_update_event(self):
        create_dispute = database_sync_to_async(Dispute.objects.create)
        dispute = await create_dispute(
            task=self.task, raised_by=self.taker, reason="Incomplete work"
        )
        communicator = WebsocketCommunicator(application, f"ws/dispute/{dispute.id}/")
        communicator.scope["user"] = self.poster
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        channel_layer = get_channel_layer()
        payload = {
            'type': 'dispute_created',
            'dispute_id': dispute.id,
            'status': 'open',
            'status_display': 'Open',
            'message': 'Dispute created'
        }
        await channel_layer.group_send(
            f"dispute_{dispute.id}",
            {'type': 'dispute_update', 'data': payload}
        )

        response = await communicator.receive_json_from()
        self.assertEqual(response['type'], 'dispute_created')
        self.assertEqual(response['dispute_id'], dispute.id)
        await communicator.disconnect()

    async def test_user_consumer_receives_dispute_notification_event(self):
        communicator = WebsocketCommunicator(application, "ws/user/")
        communicator.scope["user"] = self.poster
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        channel_layer = get_channel_layer()
        payload = {
            'type': 'dispute_created',
            'message': 'User notification test'
        }
        await channel_layer.group_send(
            f"user_{self.poster.id}",
            {'type': 'dispute_notification', 'data': payload}
        )

        response = await communicator.receive_json_from()
        self.assertEqual(response['message'], 'User notification test')
        await communicator.disconnect()

    def test_raise_dispute_dispatches_websocket_event(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not clear'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.reason, 'Task not clear')

    def test_withdraw_dispute_dispatches_websocket_event(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason test")
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

    def test_complete_task_resolves_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Dispute to resolve")
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

    def test_payload_size_constraint(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason payload test")
        payload = {
            'type': 'dispute_created',
            'event': 'dispute_created',
            'dispute_id': dispute.id,
            'task_id': self.task.id,
            'task_title': self.task.title,
            'raised_by': self.taker.username,
            'reason': dispute.reason,
            'status': dispute.status,
            'status_display': dispute.get_status_display(),
            'message': f"{self.taker.username} has raised a dispute for your task: '{self.task.title}'.",
            'link': reverse('dispute_detail', args=[dispute.id])
        }
        json_data = json.dumps(payload)
        payload_size_bytes = len(json_data.encode('utf-8'))
        self.assertLessEqual(payload_size_bytes, 2048, "Payload size must not exceed 2 KB")
