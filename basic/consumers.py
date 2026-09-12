import json
from channels.generic.websocket import AsyncWebsocketConsumer

class DisputeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        self.dispute_id = self.scope.get("url_route", {}).get("kwargs", {}).get("dispute_id")
        self.user_group_name = f"user_{self.user.id}"
        self.dispute_group_name = f"dispute_{self.dispute_id}" if self.dispute_id else None

        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name
        )

        if self.dispute_group_name:
            await self.channel_layer.group_add(
                self.dispute_group_name,
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

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            try:
                data = json.loads(text_data)
            except Exception:
                pass

    async def dispute_message(self, event):
        await self.send(text_data=json.dumps(event))

    async def notification_message(self, event):
        await self.send(text_data=json.dumps(event))
