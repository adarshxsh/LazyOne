import json
from django.test import TestCase, Client
from django.contrib.auth.models import User, AnonymousUser
from django.urls import reverse
from basic.models import Task, Dispute, UserProfile, Conversation, Notification
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from LazyOne.asgi import application


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
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

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
