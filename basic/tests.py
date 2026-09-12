from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from channels.testing import WebsocketCommunicator
from channels.layers import get_channel_layer
from asgiref.sync import sync_to_async
from LazyOne.asgi import application
from basic.models import Task, Dispute, Notification, UserProfile

class ChannelsWebSocketTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            reward=100
        )
        self.client = Client()

    async def test_websocket_consumer_dispute_and_notification(self):
        communicator = WebsocketCommunicator(
            application,
            "/ws/dispute/1/"
        )
        communicator.scope['user'] = self.taker
        connected, subprotocol = await communicator.connect()
        self.assertTrue(connected)

        channel_layer = get_channel_layer()
        await channel_layer.group_send(
            "dispute_1",
            {
                "type": "dispute_message",
                "event_type": "dispute_updated",
                "status": "open",
                "status_display": "Open"
            }
        )

        response = await communicator.receive_json_from()
        self.assertEqual(response["event_type"], "dispute_updated")
        self.assertEqual(response["status"], "open")

        await channel_layer.group_send(
            f"user_{self.taker.id}",
            {
                "type": "notification_message",
                "event_type": "notification_event",
                "message": "New dispute raised"
            }
        )

        response = await communicator.receive_json_from()
        self.assertEqual(response["event_type"], "notification_event")
        self.assertEqual(response["message"], "New dispute raised")

        await communicator.disconnect()

    async def test_raise_dispute_broadcast(self):
        communicator = WebsocketCommunicator(
            application,
            "/ws/notifications/"
        )
        communicator.scope['user'] = self.poster
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        def trigger_post():
            self.client.login(username='taker', password='password123')
            return self.client.post(
                reverse('raise_dispute', args=[self.task.id]),
                {'reason': 'Incomplete work'}
            )

        response = await sync_to_async(trigger_post)()
        self.assertEqual(response.status_code, 302)

        msg = await communicator.receive_json_from(timeout=2)
        self.assertEqual(msg["event_type"], "notification_event")
        await communicator.disconnect()

    async def test_withdraw_dispute_broadcast(self):
        def create_dispute():
            d = Dispute.objects.create(task=self.task, raised_by=self.taker, reason="Reason")
            self.task.status = 'disputed'
            self.task.save()
            return d

        dispute = await sync_to_async(create_dispute)()

        communicator = WebsocketCommunicator(
            application,
            f"/ws/dispute/{dispute.id}/"
        )
        communicator.scope['user'] = self.taker
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        def trigger_withdraw():
            self.client.login(username='taker', password='password123')
            return self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        response = await sync_to_async(trigger_withdraw)()
        self.assertEqual(response.status_code, 302)

        msg = await communicator.receive_json_from(timeout=2)
        self.assertEqual(msg["event_type"], "dispute_updated")
        self.assertEqual(msg["status"], "withdrawn")
        await communicator.disconnect()

    async def test_unauthenticated_websocket_connection_rejected(self):
        communicator = WebsocketCommunicator(
            application,
            "/ws/notifications/"
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
