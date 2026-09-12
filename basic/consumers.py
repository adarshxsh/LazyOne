import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


class DisputeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.dispute_id = self.scope.get('url_route', {}).get('kwargs', {}).get('dispute_id')
        if not self.dispute_id:
            await self.close()
            return

        is_authorized = await self.check_dispute_authorization(user, self.dispute_id)
        if not is_authorized:
            await self.close()
            return

        self.room_group_name = f"dispute_{self.dispute_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            try:
                data = json.loads(text_data)
                if data.get('type') == 'ping':
                    await self.send(text_data=json.dumps({'type': 'pong'}))
            except Exception:
                pass

    async def dispute_update(self, event):
        payload = event.get('data', event)
        await self.send(text_data=json.dumps(payload))

    @database_sync_to_async
    def check_dispute_authorization(self, user, dispute_id):
        from .models import Dispute
        try:
            dispute = Dispute.objects.select_related('task__posted_by', 'task__taken_by').get(id=dispute_id)
            task = dispute.task
            if user == task.posted_by or (task.taken_by and user == task.taken_by) or user.is_staff:
                return True
            if hasattr(dispute, 'jury_assignments') and dispute.jury_assignments.filter(juror=user).exists():
                return True
            return False
        except Dispute.DoesNotExist:
            return False


class UserConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.user_group_name = f"user_{user.id}"

        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'user_group_name'):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            try:
                data = json.loads(text_data)
                if data.get('type') == 'ping':
                    await self.send(text_data=json.dumps({'type': 'pong'}))
            except Exception:
                pass

    async def dispute_notification(self, event):
        payload = event.get('data', event)
        await self.send(text_data=json.dumps(payload))

    async def user_notification(self, event):
        payload = event.get('data', event)
        await self.send(text_data=json.dumps(payload))
