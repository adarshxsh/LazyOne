"""Chat messages: Postgres first, realtime mirror after commit (ARCHITECTURE_V2 §4.4)."""
from django.db import transaction
from django.utils import timezone

from outbox.services import emit

from ..models import Conversation, Message
from .marketplace import TransitionError
from .permissions import ChatService

MAX_LEN = 2000


def send(conversation, user, content):
    content = (content or '').strip()
    if not content:
        raise TransitionError("Message is empty.")
    if not ChatService.can_send(user, conversation):
        raise PermissionError("You are not authorized to send messages in this chat.")
    with transaction.atomic():
        msg = Message.objects.create(conversation=conversation, sender=user, content=content[:MAX_LEN])
        Conversation.objects.filter(pk=conversation.pk).update(last_message_at=timezone.now())
        emit('message.created', {'message_id': msg.pk})
    return msg


def serialize(msg):
    return {
        'id': msg.pk,
        'authorId': str(msg.sender_id) if msg.sender_id else None,
        'authorUsername': msg.sender.username if msg.sender_id else 'Deleted user',
        'content': msg.content,
        'createdAt': msg.timestamp.isoformat(),
    }


def history(conversation, after_id=None, limit=100):
    qs = conversation.messages.select_related('sender').filter(is_deleted=False)
    if after_id:
        qs = qs.filter(pk__gt=after_id)
        return [serialize(m) for m in qs.order_by('pk')[:limit]]
    latest = list(qs.order_by('-pk')[:limit])
    return [serialize(m) for m in reversed(latest)]
