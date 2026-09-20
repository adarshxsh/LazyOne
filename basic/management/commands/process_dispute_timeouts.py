from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, DisputeJurorAssignment, Task, RewardLedger, Notification, User


class Command(BaseCommand):
    help = 'Processes expired juror response windows and expired dispute voting deadlines.'

    def handle(self, *args, **options):
        now = timezone.now()

        # 1. Process expired juror response windows
        timed_out_assignments = DisputeJurorAssignment.objects.filter(
            status='assigned',
            response_deadline__lte=now,
            dispute__status='open'
        )

        expired_juror_count = 0
        replacement_count = 0

        for assignment in timed_out_assignments:
            dispute = assignment.dispute
            task = dispute.task

            with transaction.atomic():
                assignment.status = 'timed_out'
                assignment.save()
                expired_juror_count += 1

                dispute_link = reverse('dispute_detail', args=[dispute.id])

                # Notify juror of timeout
                Notification.objects.create(
                    recipient=assignment.juror,
                    message=f"Your response window for dispute on task '{task.title}' has expired and your juror assignment timed out.",
                    link=dispute_link
                )

                # Attempt replacement juror selection if dispute voting deadline is still in the future
                if dispute.voting_deadline and dispute.voting_deadline > now:
                    excluded_user_ids = set()
                    if task.posted_by_id:
                        excluded_user_ids.add(task.posted_by_id)
                    if task.taken_by_id:
                        excluded_user_ids.add(task.taken_by_id)
                    if dispute.raised_by_id:
                        excluded_user_ids.add(dispute.raised_by_id)

                    assigned_juror_ids = dispute.juror_assignments.values_list('juror_id', flat=True)
                    excluded_user_ids.update(assigned_juror_ids)

                    replacement_user = User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids).order_by('id').first()

                    if replacement_user:
                        DisputeJurorAssignment.objects.create(
                            dispute=dispute,
                            juror=replacement_user,
                            assigned_at=now,
                            response_deadline=now + timedelta(hours=dispute.juror_response_window_hours),
                            status='assigned'
                        )
                        replacement_count += 1

                        Notification.objects.create(
                            recipient=replacement_user,
                            message=f"You have been assigned as a replacement juror for dispute on task '{task.title}'.",
                            link=dispute_link
                        )

        # 2. Process expired dispute voting deadlines
        expired_disputes = Dispute.objects.filter(status='open', voting_deadline__lte=now)
        processed_disputes_count = 0

        for dispute in expired_disputes:
            task = dispute.task

            with transaction.atomic():
                voted_assignments = dispute.juror_assignments.filter(status='voted')
                voted_count = voted_assignments.count()
                dispute_link = reverse('dispute_detail', args=[dispute.id])

                if voted_count >= dispute.quorum_threshold:
                    # Quorum met: calculate majority choice
                    poster_votes = voted_assignments.filter(vote_choice='poster').count()
                    taker_votes = voted_assignments.filter(vote_choice='taker').count()

                    if taker_votes > poster_votes:
                        # Taker wins
                        task.status = 'completed'
                        task.save()

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

                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'")

                        dispute.status = 'resolved'
                        dispute.save()

                        msg = f"Dispute for task '{task.title}' reached voting deadline and was resolved in favor of the taker."
                    else:
                        # Poster wins or tie
                        task.status = 'cancelled'
                        task.save()

                        poster_profile = task.posted_by.userprofile
                        poster_profile.rewards += task.reward
                        poster_profile.save()

                        RewardLedger.objects.create(
                            user=task.posted_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_cancellation',
                            description=f"Refunded task reward for resolved dispute on task: '{task.title}'"
                        )

                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'")

                        dispute.status = 'resolved'
                        dispute.save()

                        msg = f"Dispute for task '{task.title}' reached voting deadline and was resolved in favor of the poster."

                    participants = [task.posted_by]
                    if task.taken_by and task.taken_by not in participants:
                        participants.append(task.taken_by)

                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=msg,
                            link=dispute_link
                        )

                else:
                    # Quorum NOT met: Fallback Resolution
                    task.status = 'cancelled'
                    task.save()

                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Fallback settlement: Task reward refunded due to missed voting quorum on task '{task.title}'"
                    )

                    if dispute.escrow_status == 'held':
                        dispute.refund_deposit(
                            reason_description=f"Fallback settlement: Security deposit bond refunded due to missed voting quorum on task '{task.title}'"
                        )

                    dispute.status = 'resolved'
                    dispute.save()

                    fallback_msg = f"Dispute for task '{task.title}' reached voting deadline without meeting quorum ({voted_count}/{dispute.quorum_threshold} votes). Fallback settlement executed."

                    recipients = set()
                    recipients.add(task.posted_by)
                    if task.taken_by:
                        recipients.add(task.taken_by)

                    assigned_juror_users = User.objects.filter(dispute_assignments__dispute=dispute)
                    for juror in assigned_juror_users:
                        recipients.add(juror)

                    for recipient in recipients:
                        Notification.objects.create(
                            recipient=recipient,
                            message=fallback_msg,
                            link=dispute_link
                        )

                processed_disputes_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Processed {expired_juror_count} timed-out juror(s), assigned {replacement_count} replacement(s), and resolved {processed_disputes_count} expired dispute(s)."
            )
        )
