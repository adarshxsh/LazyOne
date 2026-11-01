"""
Outbox handlers: every side effect of a domain change lives here, not in views.
Each handler must be idempotent-ish: it may run more than once on retry.
"""
from django.urls import reverse

from outbox.services import handler

from .models import Conversation, Dispute, Message, Notification, Task, realtime_uid
from .realtime import get_publisher


def notify(user, message, link=None):
    if user is not None:
        Notification.objects.create(recipient=user, message=message[:255], link=link)


TASK_MESSAGES = {
    # action: (recipient attr, message template)
    'accept': ('posted_by', "{actor} has taken your task: '{title}'"),
    'submit': ('posted_by', "{actor} submitted '{title}' for approval. It auto-approves in 48h."),
    'approve': ('taken_by', "'{title}' was approved. {reward} points are yours!"),
    'auto_approve': ('taken_by', "'{title}' was auto-approved. {reward} points are yours!"),
    'abandon': ('posted_by', "{actor} abandoned your task '{title}'. It is open again."),
    'request_cancel': ('taken_by', "{actor} has requested to cancel the task: '{title}'."),
    'accept_cancel': ('posted_by', "{actor} accepted your cancellation for '{title}'. Your points were refunded."),
    'expire': ('posted_by', "'{title}' expired without a taker. Your points were refunded."),
}


@handler('task.transitioned')
def on_task_transitioned(payload):
    action = payload['action']
    if action not in TASK_MESSAGES:
        return
    task = Task.objects.select_related('posted_by', 'taken_by').filter(pk=payload['task_id']).first()
    if task is None:
        return
    attr, template = TASK_MESSAGES[action]
    recipient = getattr(task, attr)
    if recipient is None and action == 'abandon':
        recipient = task.posted_by
    from django.contrib.auth.models import User
    actor = User.objects.filter(pk=payload.get('actor_id')).first()
    notify(recipient, template.format(actor=getattr(actor, 'username', 'Someone'), title=task.title,
                                      reward=task.reward), reverse('my_tasks'))


@handler('task.wave')
def on_task_wave(payload):
    from .services.feed import wave_recipients
    task = Task.objects.select_related('posted_by').filter(pk=payload['task_id']).first()
    if task is None or task.status != Task.Status.AVAILABLE or task.opened_at.isoformat() != payload['opened_at']:
        return  # taken, cancelled or re-opened since this wave was scheduled
    for user in wave_recipients(task, payload['wave']):
        notify(user, f"{task.posted_by.username} needs a hand: '{task.title}' ({task.reward} pts)", reverse('home'))


@handler('message.created')
def on_message_created(payload):
    from .services.messaging import serialize
    msg = Message.objects.select_related('sender', 'conversation').filter(pk=payload['message_id']).first()
    if msg is None:
        return
    conv = msg.conversation
    get_publisher().publish_message(conv.public_id, serialize(msg))
    link = reverse('chat_view', args=[conv.public_id])
    for user in conv.participants.exclude(pk=msg.sender_id):
        # one unread "new message" notification per chat is enough
        if not Notification.objects.filter(recipient=user, link=link, is_read=False).exists():
            notify(user, f"New message from {msg.sender.username if msg.sender else 'someone'}", link)


@handler('conversation.members_changed')
def on_members_changed(payload):
    from .services.permissions import ChatService
    if 'conversation_id' in payload:
        conv = Conversation.objects.filter(pk=payload['conversation_id']).first()
    else:
        conv = Conversation.objects.filter(task_id=payload['task_id']).first()
    if conv is None:
        return
    get_publisher().set_conversation_members(conv.public_id, [realtime_uid(uid) for uid in ChatService.reader_ids(conv)])


def _dispute(payload):
    return Dispute.objects.select_related('task__posted_by', 'task__taken_by', 'raised_by').filter(
        pk=payload['dispute_id']).first()


def _other_party(d, user_id):
    return d.task.posted_by if user_id == d.task.taken_by_id else d.task.taken_by


@handler('dispute.opened')
def on_dispute_opened(payload):
    d = _dispute(payload)
    if d is None:
        return
    link = reverse('dispute_detail', args=[d.public_id])
    notify(_other_party(d, d.raised_by_id),
           f"{d.raised_by.username} raised a dispute on '{d.task.title}'. Accept, counter or reject their offer "
           f"within 24 h, or it is accepted automatically.", link)


@handler('dispute.offer')
def on_dispute_offer(payload):
    from .models import SettlementOffer
    d = _dispute(payload)
    offer = SettlementOffer.objects.select_related('proposed_by').filter(pk=payload.get('offer_id')).first()
    if d and offer and offer.proposed_by:
        notify(_other_party(d, offer.proposed_by_id),
               f"{offer.proposed_by.username} offered a settlement on '{d.task.title}': taker gets {offer.taker_pct}%.",
               reverse('dispute_detail', args=[d.public_id]))


@handler('dispute.jury')
def on_dispute_jury(payload):
    d = _dispute(payload)
    if d is None:
        return
    link = reverse('dispute_detail', args=[d.public_id])
    label = "appeal jury" if payload.get('round') == 2 else "jury"
    for seat in d.jurors.filter(round=payload.get('round', 1)).select_related('juror'):
        notify(seat.juror, f"You were selected for the {label} on '{d.task.title}'. Review the evidence and vote.", link)
    for user in {d.task.posted_by, d.task.taken_by} - {None}:
        notify(user, f"'{d.task.title}' went to a {label}. You can still add evidence.", link)


@handler('dispute.decided')
def on_dispute_decided(payload):
    d = _dispute(payload)
    if d is None:
        return
    link = reverse('dispute_detail', args=[d.public_id])
    for user in {d.task.posted_by, d.task.taken_by} - {None}:
        notify(user, f"The jury decided '{d.task.title}': taker gets {d.outcome_pct}%. Appeal within 24 h if you disagree.",
               link)


@handler('dispute.appealed')
def on_dispute_appealed(payload):
    d = _dispute(payload)
    if d and d.appealed_by_id:
        notify(_other_party(d, d.appealed_by_id),
               f"The verdict on '{d.task.title}' was appealed. A larger jury will decide.",
               reverse('dispute_detail', args=[d.public_id]))


@handler('dispute.escalated')
def on_dispute_escalated(payload):
    d = Dispute.objects.select_related('task').filter(pk=payload['dispute_id']).first()
    if d:
        _notify_staff(d, reverse('dispute_detail', args=[d.public_id]))


def _notify_staff(dispute, link):
    from django.contrib.auth.models import User
    for staff in User.objects.filter(is_staff=True, is_active=True):
        notify(staff, f"Dispute on '{dispute.task.title}' needs a moderator.", link)


@handler('dispute.resolved')
def on_dispute_resolved(payload):
    d = Dispute.objects.select_related('task__posted_by', 'task__taken_by').filter(pk=payload['dispute_id']).first()
    if d is None:
        return
    link = reverse('dispute_detail', args=[d.public_id])
    share = f" (taker {d.outcome_pct}%)" if d.outcome_pct is not None else ""
    msg = f"The dispute on '{d.task.title}' is closed: {d.get_outcome_display() or d.get_status_display()}{share}."
    for user in {d.task.posted_by, d.task.taken_by} - {None}:
        notify(user, msg, link)
