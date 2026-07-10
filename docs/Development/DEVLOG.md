# Developer Log

## 2025-11-16: AWS Deployment
- Finished configuring Django for AWS deployment.
- Set up secure host configurations and static assets delivery.

## 2025-10-06: Interactive Features & Websockets
- Integrated Django Channels with Redis as the backing store for real-time WebSocket communication in chats.
- Configured channel layer routing for WebSocket connections (`basic/routing.py`, `basic/consumers.py`).
- Implemented Firebase Phone and Instagram OAuth verification integrations.
- Built interactive frontend visualization representing user networks ("Social Circle").
- Added friend requests with customized closeness meters and status tracking.