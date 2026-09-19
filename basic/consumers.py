import json
from channels.generic.websocket import AsyncJsonWebsocketConsumer

class DisputeConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        url_kwargs = self.scope.get('url_route', {}).get('kwargs', {})
        self.dispute_id = url_kwargs.get('dispute_id')
        self.groups_to_join = ['disputes_global']
        if self.dispute_id:
            self.groups_to_join.append(f'dispute_{self.dispute_id}')

        for group in self.groups_to_join:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'groups_to_join'):
            for group in self.groups_to_join:
                await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if isinstance(content, dict) and content.get('type') == 'ping':
            await self.send_json({'type': 'pong'})

    async def dispute_update(self, event):
        await self.send_json(event)

