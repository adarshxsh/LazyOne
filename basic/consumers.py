import json
from channels.generic.websocket import AsyncWebsocketConsumer

class DisputeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.dispute_id = self.scope['url_route']['kwargs'].get('dispute_id')
        self.group_name = f'dispute_{self.dispute_id}'

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def dispute_update(self, event):
        payload = event.get('payload', event)
        if isinstance(payload, dict):
            data_to_send = {k: v for k, v in payload.items() if k != 'type'}
        else:
            data_to_send = payload
        await self.send(text_data=json.dumps(data_to_send))

