from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from .models import Dispute, RewardLedger, Notification

def process_expired_disputes():
    """
    Identifies all open disputes past their expiration deadline,
    applies default resolution rules, updates task and dispute statuses,
    transfers/refunds escrow reward points atomically, and notifies both parties.
    Returns the count of disputes processed.
    """
    now = timezone.now()
    expired_disputes = Dispute.objects.filter(
        status='open',
        expires_at__isnull=False,
        expires_at__lte=now
    )
    processed_count = 0

    for dispute in expired_disputes:
        task = dispute.task
        posted_by = task.posted_by
        taken_by = task.taken_by

        with transaction.atomic():
            if dispute.raised_by == taken_by:
                counter_party = posted_by
                raiser = taken_by
            else:
                counter_party = taken_by
                raiser = posted_by

            counter_party_submitted_evidence = dispute.evidences.filter(submitted_by=counter_party).exists()

            if counter_party_submitted_evidence:
                dispute.status = 'resolved'
                dispute.save()
                task.status = 'cancelled'
                task.save()

                if posted_by and hasattr(posted_by, 'userprofile'):
                    poster_profile = posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Auto-resolved dispute refund for task: '{task.title}'"
                    )
            else:
                dispute.status = 'resolved'
                dispute.save()

                if raiser == taken_by:
                    task.status = 'completed'
                    task.save()

                    if taken_by and hasattr(taken_by, 'userprofile'):
                        taker_profile = taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Auto-resolved dispute payout for task: '{task.title}'"
                        )
                else:
                    task.status = 'cancelled'
                    task.save()

                    if posted_by and hasattr(posted_by, 'userprofile'):
                        poster_profile = posted_by.userprofile
                        poster_profile.rewards += task.reward
                        poster_profile.save()

                        RewardLedger.objects.create(
                            user=posted_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_cancellation',
                            description=f"Auto-resolved dispute refund for task: '{task.title}'"
                        )

            dispute_link = reverse('dispute_detail', args=[dispute.id])
            if posted_by:
                Notification.objects.create(
                    recipient=posted_by,
                    message=f"Dispute for task '{task.title}' reached expiration deadline and has been auto-resolved.",
                    link=dispute_link
                )
            if taken_by:
                Notification.objects.create(
                    recipient=taken_by,
                    message=f"Dispute for task '{task.title}' reached expiration deadline and has been auto-resolved.",
                    link=dispute_link
                )

            processed_count += 1

    return processed_count
