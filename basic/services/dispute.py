from django.db import transaction
from django.urls import reverse
from django.core.exceptions import ValidationError, PermissionDenied
from ..models import Dispute, DisputeAuditEvent, Task, Notification, RewardLedger, UserProfile

class DisputeService:

    @staticmethod
    def log_audit_event(dispute, event_type, actor=None, details=None, message=None):
        """
        Creates an immutable DisputeAuditEvent record and dispatches Notifications to all
        relevant participants (litigants and assigned jurors).
        """
        if details is None:
            details = {}

        event = DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=actor,
            event_type=event_type,
            details_json=details
        )

        if message:
            recipients = set()
            task = dispute.task
            if task.posted_by:
                recipients.add(task.posted_by)
            if task.taken_by:
                recipients.add(task.taken_by)

            # Add assigned jurors
            for juror in dispute.jurors.all():
                recipients.add(juror)

            dispute_link = reverse('dispute_detail', args=[dispute.id])
            for recipient in recipients:
                Notification.objects.create(
                    recipient=recipient,
                    message=message,
                    link=dispute_link
                )

        return event

    @staticmethod
    def raise_dispute(task, user, reason):
        if hasattr(task, 'dispute') and task.dispute.status == 'open':
            return task.dispute

        if user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
            raise ValidationError("You can only raise a dispute for a task you are participating in that is in progress.")

        if not reason:
            raise ValidationError("A reason is required to raise a dispute.")

        deposit_amount = task.deposit_bond_amount
        user_profile = user.userprofile
        if user_profile.rewards < deposit_amount:
            raise ValidationError(
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            RewardLedger.objects.create(
                user=user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            DisputeService.log_audit_event(
                dispute=dispute,
                event_type='dispute_raised',
                actor=user,
                details={'reason': reason, 'deposit_amount': deposit_amount},
                message=f"{user.username} has raised a dispute for task: '{task.title}'."
            )

        return dispute

    @staticmethod
    def withdraw_dispute(dispute, user):
        if dispute.raised_by != user:
            raise PermissionDenied("You are not authorized to withdraw this dispute.")

        if dispute.status != 'open':
            raise ValidationError("Only open disputes can be withdrawn.")

        task = dispute.task
        with transaction.atomic():
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'in_progress'
            task.save()

            DisputeService.log_audit_event(
                dispute=dispute,
                event_type='dispute_withdrawn',
                actor=user,
                details={'refunded_amount': dispute.deposit_amount},
                message=f"{user.username} has withdrawn the dispute for '{task.title}'."
            )

        return dispute

    @staticmethod
    def resolve_dispute(dispute, resolver, outcome):
        if not resolver.is_staff and resolver not in [dispute.task.posted_by, dispute.task.taken_by]:
            raise PermissionDenied("You are not authorized to resolve this dispute.")

        if dispute.status != 'open':
            raise ValidationError("Dispute is already resolved or withdrawn.")

        task = dispute.task
        with transaction.atomic():
            if outcome == 'posted_by':
                # Favor poster: cancel task, refund task reward to poster
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                task.status = 'cancelled'
                task.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for resolved dispute on task: '{task.title}'"
                )

                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited to poster for resolved dispute on task: '{task.title}'")

            elif outcome == 'taken_by':
                # Favor taker: complete task, award reward to taker
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded reward for resolved dispute on task: '{task.title}'"
                    )

                task.status = 'completed'
                task.save()

                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited to taker for resolved dispute on task: '{task.title}'")
            else:
                raise ValidationError("Invalid resolution outcome.")

            dispute.status = 'resolved'
            dispute.save()

            DisputeService.log_audit_event(
                dispute=dispute,
                event_type='dispute_resolved',
                actor=resolver,
                details={'outcome': outcome},
                message=f"Dispute for task '{task.title}' was resolved in favor of {outcome}."
            )

        return dispute
