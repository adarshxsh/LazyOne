from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, DisputeAuditEvent, Notification, RewardLedger

class DisputeLifecycleService:

    @classmethod
    def _notify_participants(cls, task, dispute, message):
        """
        Sends Notification records to all task participants (poster and taker).
        """
        participants = []
        if task.posted_by:
            participants.append(task.posted_by)
        if task.taken_by and task.taken_by not in participants:
            participants.append(task.taken_by)

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for participant in participants:
            Notification.objects.create(
                recipient=participant,
                message=message,
                link=dispute_link
            )

    @classmethod
    def raise_dispute(cls, task, actor, reason):
        """
        Executes dispute creation / reopening state transition inside atomic transaction.
        Deducts deposit bond, updates/creates Dispute, creates RewardLedger,
        updates Task status, logs DisputeAuditEvent, and notifies task participants.
        """
        if task.taken_by != actor or task.status != 'in_progress':
            raise ValueError("Disputes can only be raised for tasks in progress by the task taker.")

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

            state_before = None
            if hasattr(task, 'dispute'):
                dispute = task.dispute
                state_before = dispute.status
                dispute.raised_by = actor
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
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
                event_type='raised',
                state_before=state_before,
                state_after='open',
                metadata={
                    'deposit_amount': deposit_amount,
                    'reason': reason,
                    'raised_by': actor.username
                }
            )

            cls._notify_participants(
                task=task,
                dispute=dispute,
                message=f"{actor.username} has raised a dispute for task: '{task.title}'."
            )

            return dispute

    @classmethod
    def withdraw_dispute(cls, dispute, actor):
        """
        Executes dispute withdrawal inside atomic transaction.
        Refunds deposit bond, updates dispute status to 'resolved', updates task status to 'in_progress',
        logs DisputeAuditEvent, and notifies task participants.
        """
        if dispute.raised_by != actor:
            raise ValueError("Only the user who raised the dispute can withdraw it.")

        task = dispute.task
        with transaction.atomic():
            state_before = dispute.status
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'in_progress'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=actor,
                event_type='withdrawn',
                state_before=state_before,
                state_after='resolved',
                metadata={
                    'action': 'withdrawn',
                    'withdrawn_by': actor.username
                }
            )

            cls._notify_participants(
                task=task,
                dispute=dispute,
                message=f"{actor.username} has withdrawn the dispute for '{task.title}'. The task is now in progress."
            )

            return dispute

    @classmethod
    def resolve_on_task_completion(cls, dispute, actor):
        """
        Executes dispute resolution when a task poster completes a disputed task inside atomic transaction.
        Refunds deposit bond, updates dispute status to 'resolved', logs DisputeAuditEvent, and notifies participants.
        """
        task = dispute.task
        with transaction.atomic():
            state_before = dispute.status
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=actor,
                event_type='resolved_on_task_completion',
                state_before=state_before,
                state_after='resolved',
                metadata={
                    'resolved_via': 'task_completion',
                    'completed_by': actor.username if actor else 'system'
                }
            )

            cls._notify_participants(
                task=task,
                dispute=dispute,
                message=f"Dispute for task '{task.title}' was resolved upon task completion."
            )

            return dispute

    @classmethod
    def resolve_expired_dispute(cls, dispute, days=7):
        """
        Executes SLA automated dispute resolution inside atomic transaction.
        Handles deposit refund/forfeiture, updates task reward and status (cancelled or completed),
        logs DisputeAuditEvent with null actor and system flag metadata, and notifies participants.
        """
        task = dispute.task
        with transaction.atomic():
            state_before = dispute.status
            dispute.status = 'resolved'
            dispute.save()

            if dispute.raised_by == task.posted_by:
                # Poster challenged an unresponsive taker: cancel task, refund task reward, handle bond
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
                    description=f"Refund for expired dispute on task: '{task.title}'"
                )

                if dispute.escrow_status == 'held':
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                resolution_outcome = 'cancelled'
            else:
                # Taker raised dispute: award reward to taker, complete task, handle bond
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()

                if dispute.escrow_status == 'held':
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                resolution_outcome = 'completed'

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=None,
                event_type='sla_auto_resolved',
                state_before=state_before,
                state_after='resolved',
                metadata={
                    'system': True,
                    'sla_days': days,
                    'resolution_outcome': resolution_outcome,
                    'triggered_by': 'SLA Manager'
                }
            )

            cls._notify_participants(
                task=task,
                dispute=dispute,
                message=f"Dispute for task '{task.title}' has expired ({days}d SLA) and was automatically resolved."
            )

            return dispute
