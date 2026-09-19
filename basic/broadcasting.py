import asyncio
from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer

def _build_payload(dispute, event_type, message):
    conversation_id = None
    try:
        if hasattr(dispute.task, 'conversation') and dispute.task.conversation:
            conversation_id = dispute.task.conversation.id
    except Exception:
        pass

    return {
        "type": "dispute_update",
        "event": event_type,
        "message": message,
        "dispute": {
            "id": dispute.id,
            "status": dispute.status,
            "status_display": dispute.get_status_display(),
            "escrow_status": dispute.escrow_status,
            "escrow_status_display": dispute.get_escrow_status_display(),
            "deposit_amount": dispute.deposit_amount,
            "reason": dispute.reason,
            "raised_by": dispute.raised_by.username if dispute.raised_by else "",
            "raised_by_id": dispute.raised_by.id if dispute.raised_by else None,
            "created_at": dispute.created_at.strftime("%B %d, %Y") if dispute.created_at else "",
        },
        "task": {
            "id": dispute.task.id,
            "title": dispute.task.title,
            "status": dispute.task.status,
            "status_display": dispute.task.get_status_display(),
            "reward": dispute.task.reward,
            "posted_by": dispute.task.posted_by.username if dispute.task.posted_by else "",
            "posted_by_id": dispute.task.posted_by.id if dispute.task.posted_by else None,
            "taken_by": dispute.task.taken_by.username if dispute.task.taken_by else None,
            "taken_by_id": dispute.task.taken_by.id if dispute.task.taken_by else None,
            "conversation_id": conversation_id,
        }
    }

def broadcast_dispute_update(dispute, event_type, message=""):
    """
    Broadcasts real-time dispute update events to channel layer groups.
    Handles both synchronous and asynchronous execution contexts safely.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        async_build = sync_to_async(_build_payload)
        async def _async_broadcast():
            payload = await async_build(dispute, event_type, message)
            await channel_layer.group_send("disputes_global", payload)
            await channel_layer.group_send(f"dispute_{dispute.id}", payload)

        asyncio.create_task(_async_broadcast())
    else:
        payload = _build_payload(dispute, event_type, message)
        async_to_sync(channel_layer.group_send)("disputes_global", payload)
        async_to_sync(channel_layer.group_send)(f"dispute_{dispute.id}", payload)
