from django.db.models.signals import pre_save, post_save
from django.dispatch import Signal, receiver
from django.urls import reverse
from django.contrib.auth.models import User
from .models import Dispute, DisputeAuditEvent, Notification

# Custom signal for explicit dispute state transition dispatches
dispute_state_changed = Signal()

@receiver(pre_save, sender=Dispute)
def dispute_pre_save(sender, instance, **kwargs):
    if instance.pk:
        old_dispute = Dispute.objects.filter(pk=instance.pk).first()
        if old_dispute:
            instance._old_status = old_dispute.status
            instance._old_escrow_status = old_dispute.escrow_status
        else:
            instance._old_status = None
            instance._old_escrow_status = None
    else:
        instance._old_status = None
        instance._old_escrow_status = None

@receiver(post_save, sender=Dispute)
def dispute_post_save(sender, instance, created, **kwargs):
    # Check if this save caused a status or escrow_status transition
    old_status = getattr(instance, '_old_status', None)
    old_escrow_status = getattr(instance, '_old_escrow_status', None)

    # Ignore unchanged model saves to avoid duplicate audit logs
    if not created and old_status == instance.status and old_escrow_status == instance.escrow_status:
        return

    # Determine actor
    if hasattr(instance, '_actor'):
        actor = instance._actor
    elif hasattr(instance, 'raised_by'):
        actor = instance.raised_by
    else:
        actor = None

    # Determine event_type and description
    event_type = getattr(instance, '_event_type', None)
    details_json = getattr(instance, '_details_json', {}) or {}
    description = getattr(instance, '_description', '') or ''

    previous_status = ''
    new_status = ''

    if created:
        if not event_type:
            event_type = 'RAISED'
        previous_status = ''
        new_status = instance.status
        if not description:
            actor_str = actor.username if actor else "System"
            description = f"{actor_str} raised a dispute for task '{instance.task.title}'."
    else:
        # State transition on existing instance
        if old_status != instance.status:
            previous_status = old_status or ''
            new_status = instance.status
            if not event_type:
                if instance.status == 'open':
                    event_type = 'RAISED'
                elif instance.status == 'resolved':
                    if getattr(instance, '_is_expired', False) or getattr(instance, '_event_type', '') == 'EXPIRED':
                        event_type = 'EXPIRED'
                        actor = None
                    elif actor == instance.raised_by:
                        event_type = 'WITHDRAWN'
                    else:
                        event_type = 'RESOLVED'
                else:
                    event_type = f"STATUS_{instance.status.upper()}"
        elif old_escrow_status != instance.escrow_status:
            previous_status = old_escrow_status or ''
            new_status = instance.escrow_status
            if not event_type:
                if instance.escrow_status == 'refunded':
                    event_type = 'ESCROW_REFUNDED'
                elif instance.escrow_status == 'forfeited':
                    event_type = 'ESCROW_FORFEITED'
                else:
                    event_type = f"ESCROW_{instance.escrow_status.upper()}"

    if not event_type:
        event_type = 'DISPUTE_UPDATED'

    if not description:
        actor_str = actor.username if actor else "System"
        if event_type in ['RAISED', 'DISPUTE_RAISED']:
            description = f"{actor_str} raised a dispute for task '{instance.task.title}'."
        elif event_type in ['WITHDRAWN', 'DISPUTE_WITHDRAWN']:
            description = f"{actor_str} withdrew the dispute for task '{instance.task.title}'."
        elif event_type in ['RESOLVED', 'DISPUTE_RESOLVED']:
            description = f"Dispute for task '{instance.task.title}' was resolved."
        elif event_type in ['EXPIRED', 'DISPUTE_EXPIRED']:
            description = f"Dispute for task '{instance.task.title}' expired and was automatically resolved."
        elif event_type in ['ESCROW_REFUNDED']:
            description = f"Escrow deposit bond refunded for dispute on task '{instance.task.title}'."
        elif event_type in ['ESCROW_FORFEITED']:
            description = f"Escrow deposit bond forfeited for dispute on task '{instance.task.title}'."
        else:
            description = f"Dispute event '{event_type}' for task '{instance.task.title}'."

    # Create immutable audit record
    DisputeAuditEvent.objects.create(
        dispute=instance,
        actor=actor,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        details_json=details_json,
        description=description
    )

    # Multi-party notification dispatch to both participants
    task = instance.task
    recipients = set()
    if task.posted_by:
        recipients.add(task.posted_by)
    if task.taken_by:
        recipients.add(task.taken_by)

    detail_link = reverse('dispute_detail', args=[instance.id])

    for recipient in recipients:
        Notification.objects.create(
            recipient=recipient,
            message=description,
            link=detail_link
        )

@receiver(dispute_state_changed)
def handle_dispute_state_changed(sender, dispute, event_type, actor=None, metadata=None, **kwargs):
    metadata = metadata or {}
    dispute._actor = actor
    dispute._event_type = event_type
    dispute._details_json = metadata
    dispute._description = metadata.get('message', '') or metadata.get('description', '')
    if dispute.pk:
        task = dispute.task
        description = dispute._description or f"Dispute event '{event_type}' for task '{task.title}'."
        DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=actor,
            event_type=event_type,
            previous_status=metadata.get('previous_status', ''),
            new_status=metadata.get('new_status', dispute.status),
            details_json=metadata,
            description=description
        )

        recipients = set()
        if task.posted_by:
            recipients.add(task.posted_by)
        if task.taken_by:
            recipients.add(task.taken_by)

        detail_link = metadata.get('link') or reverse('dispute_detail', args=[dispute.id])

        for recipient in recipients:
            Notification.objects.create(
                recipient=recipient,
                message=description,
                link=detail_link
            )
