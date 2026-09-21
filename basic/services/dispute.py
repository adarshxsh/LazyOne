from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, Task, Notification, RewardLedger, DisputeAuditEvent


class DisputeService:
    @staticmethod
    def notify_participants(dispute, message, link=None):
        if link is None:
            link = reverse('dispute_detail', args=[dispute.id])

        participants = []
        task = dispute.task
        if task.posted_by:
            participants.append(task.posted_by)
        if task.taken_by:
            participants.append(task.taken_by)
        if dispute.raised_by:
            participants.append(dispute.raised_by)

        # Deduplicate participants while preserving order
        unique_participants = list({p.id: p for p in participants if p}.values())

        for participant in unique_participants:
            Notification.objects.create(
                recipient=participant,
                message=message,
                link=link
            )

    @staticmethod
    def raise_dispute(task, actor, reason):
        if hasattr(task, 'dispute') and task.dispute.status == 'open':
            return task.dispute

        if task.taken_by != actor or task.status != 'in_progress':
            raise ValueError("You can only raise a dispute for a task you have taken that is currently in progress.")

        if not reason:
            raise ValueError("A reason is required to raise a dispute.")

        deposit_amount = task.deposit_bond_amount
        user_profile = actor.userprofile
        if user_profile.rewards < deposit_amount:
            raise ValueError(
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                previous_status = dispute.status
                dispute.raised_by = actor
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                previous_status = ''
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=actor,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    status='open'
                )

            RewardLedger.objects.create(
                user=actor,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=actor,
                event_type='RAISED',
                previous_status=previous_status,
                new_status='open',
                description=f"Dispute raised by {actor.username}: {reason}"
            )

            DisputeService.notify_participants(
                dispute=dispute,
                message=f"{actor.username} has raised a dispute for task: '{task.title}'."
            )

        return dispute

    @staticmethod
    def withdraw_dispute(dispute, actor):
        if dispute.status != 'open':
            return dispute

        task = dispute.task
        with transaction.atomic():
            previous_status = dispute.status
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'",
                actor=actor
            )
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'in_progress'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=actor,
                event_type='WITHDRAWN',
                previous_status=previous_status,
                new_status='resolved',
                description=f"Dispute withdrawn by {actor.username}."
            )

            DisputeService.notify_participants(
                dispute=dispute,
                message=f"{actor.username} has withdrawn the dispute for '{task.title}'. The task is now in progress."
            )

        return dispute

    @staticmethod
    def resolve_dispute(dispute, actor=None, description=None, beneficiary=None, refund_escrow=True):
        if dispute.status == 'resolved':
            return dispute

        task = dispute.task
        with transaction.atomic():
            previous_status = dispute.status

            if dispute.escrow_status == 'held':
                if refund_escrow:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'",
                        actor=actor
                    )
                elif beneficiary:
                    dispute.forfeit_deposit(
                        beneficiary=beneficiary,
                        reason_description=f"Security deposit bond forfeited upon dispute resolution for task: '{task.title}'",
                        actor=actor
                    )

            dispute.status = 'resolved'
            dispute.save()

            actor_str = actor.username if actor else 'System'
            desc = description or f"Dispute resolved by {actor_str}."

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=actor,
                event_type='RESOLVED',
                previous_status=previous_status,
                new_status='resolved',
                description=desc
            )

            DisputeService.notify_participants(
                dispute=dispute,
                message=f"Dispute for task '{task.title}' has been resolved."
            )

        return dispute
