from django.dispatch import Signal, receiver
from django.db import transaction
from django.urls import reverse
from django.contrib.auth.models import User
from .models import Dispute, DisputeAuditEvent, Notification

# Custom signal for dispute lifecycle events
dispute_state_changed = Signal()

@receiver(dispute_state_changed)
def handle_dispute_state_changed(sender, dispute, event_type, actor=None, metadata=None, **kwargs):
    metadata = metadata or {}

    def _record_audit_and_notify():
        # Create immutable audit record
        DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=actor,
            event_type=event_type,
            metadata=metadata
        )

        # Identify all involved participants
        task = dispute.task
        participants = set()

        if task.posted_by:
            participants.add(task.posted_by)
        if task.taken_by:
            participants.add(task.taken_by)

        # Check for explicitly passed reviewers/jurors in metadata
        reviewers = metadata.get('reviewers') or metadata.get('jurors') or []
        for reviewer in reviewers:
            if isinstance(reviewer, User):
                participants.add(reviewer)
            elif isinstance(reviewer, int):
                u = User.objects.filter(id=reviewer).first()
                if u:
                    participants.add(u)

        # Include conversation participants if any exist
        if hasattr(task, 'conversation') and task.conversation:
            for p in task.conversation.participants.all():
                participants.add(p)

        # Format message
        custom_message = metadata.get('message')
        if custom_message:
            message_text = custom_message
        else:
            actor_str = actor.username if actor else "System"
            if event_type == 'DISPUTE_RAISED':
                message_text = f"Dispute raised for task '{task.title}' by {actor_str}."
            elif event_type == 'EVIDENCE_ADDED':
                message_text = f"New evidence added to dispute for task '{task.title}' by {actor_str}."
            elif event_type == 'DISPUTE_RESOLVED':
                message_text = f"Dispute for task '{task.title}' has been resolved."
            elif event_type == 'DISPUTE_WITHDRAWN':
                message_text = f"{actor_str} has withdrawn the dispute for '{task.title}'."
            elif event_type == 'DISPUTE_EXPIRED':
                message_text = f"Dispute for task '{task.title}' has expired and was automatically resolved."
            else:
                message_text = f"Dispute state changed ({event_type}) for task '{task.title}'."

        link = metadata.get('link') or reverse('dispute_detail', args=[dispute.id])

        for participant in participants:
            Notification.objects.create(
                recipient=participant,
                message=message_text,
                link=link
            )

    transaction.on_commit(_record_audit_and_notify)
