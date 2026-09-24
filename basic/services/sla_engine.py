from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, Task, Notification, RewardLedger

class SLATimerEngine:
    """
    Multi-phase SLA Timer Engine for dispute state transitions.
    Processes automated phase progression for evidence submission, voting, and appeal phases.
    """

    @classmethod
    def process_dispute_sla_transitions(cls):
        now = timezone.now()
        active_disputes = Dispute.objects.filter(
            status__in=['open', 'evidence_submission', 'voting', 'under_appeal']
        )

        transition_counts = {
            'evidence_to_voting': 0,
            'voting_to_appeal': 0,
            'appeal_to_resolved': 0,
            'total': 0
        }

        for dispute in active_disputes:
            if not dispute.is_phase_expired():
                continue

            phase = dispute.get_current_phase()

            with transaction.atomic():
                # Re-fetch dispute with lock to ensure concurrent safety
                dispute = Dispute.objects.select_for_update().get(id=dispute.id)
                task = dispute.task
                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])

                if phase == 'evidence_submission':
                    dispute.start_voting_phase(save=True)
                    transition_counts['evidence_to_voting'] += 1
                    transition_counts['total'] += 1

                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Evidence submission phase for dispute on task '{task.title}' has closed. Voting phase is now open.",
                            link=dispute_link
                        )

                elif phase == 'voting':
                    dispute.start_appeal_phase(save=True)
                    transition_counts['voting_to_appeal'] += 1
                    transition_counts['total'] += 1

                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Voting phase for dispute on task '{task.title}' has closed. Dispute is now in the appeal window.",
                            link=dispute_link
                        )

                elif phase == 'appeal':
                    dispute.status = 'resolved'
                    dispute.save()
                    transition_counts['appeal_to_resolved'] += 1
                    transition_counts['total'] += 1

                    # Finalize financial escrow settlement if still held
                    if dispute.escrow_status == 'held':
                        if dispute.raised_by == task.posted_by:
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
                                description=f"Refund for expired SLA dispute on task: '{task.title}'"
                            )

                            dispute.refund_deposit(
                                reason_description=f"Deposit bond refunded on SLA-resolved dispute for task '{task.title}'"
                            )
                        else:
                            if task.taken_by:
                                taker_profile = task.taken_by.userprofile
                                taker_profile.rewards += task.reward
                                taker_profile.save()

                                RewardLedger.objects.create(
                                    user=task.taken_by,
                                    task=task,
                                    amount=task.reward,
                                    transaction_type='task_completion',
                                    description=f"Awarded reward for SLA-resolved dispute on task: '{task.title}'"
                                )
                            task.status = 'completed'
                            task.save()

                            dispute.refund_deposit(
                                reason_description=f"Deposit bond refunded on SLA-resolved dispute for task '{task.title}'"
                            )

                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Appeal window for dispute on task '{task.title}' has expired. Dispute resolution is now final.",
                            link=dispute_link
                        )

        return transition_counts
