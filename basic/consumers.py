import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Dispute

class DisputeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        url_kwargs = self.scope.get('url_route', {}).get('kwargs', {})
        dispute_id = url_kwargs.get('dispute_id')

        self.dispute_group_name = None
        if dispute_id is not None:
            authorized = await self.is_authorized_dispute_participant(dispute_id)
            if not authorized:
                await self.close()
                return
            self.dispute_group_name = f"dispute_{dispute_id}"
            await self.channel_layer.group_add(
                self.dispute_group_name,
                self.channel_name
            )

        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'dispute_group_name') and self.dispute_group_name:
            await self.channel_layer.group_discard(
                self.dispute_group_name,
                self.channel_name
            )
        if hasattr(self, 'user_group_name') and self.user_group_name:
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name
            )

    async def dispute_updated(self, event):
        await self.send(text_data=json.dumps(event))

    async def notification_event(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def is_authorized_dispute_participant(self, dispute_id):
        try:
            dispute = Dispute.objects.select_related('task__posted_by', 'task__taken_by').get(id=dispute_id)
            task = dispute.task
            if self.user.is_staff or self.user == task.posted_by or self.user == task.taken_by:
                return True
            return False
        except Dispute.DoesNotExist:
            return False
