from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, UserProfile, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Automatically resolves open disputes whose voting deadline has expired, refunds locked points to task posters, and cancels associated tasks.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired_disputes = Dispute.objects.filter(
            status='open',
            voting_deadline__lte=now
        )

        resolved_count = 0
        for dispute in expired_disputes:
            try:
                with transaction.atomic():
                    task = dispute.task
                    dispute.status = 'resolved'
                    dispute.save()

                    task.status = 'cancelled'
                    task.save()

                    # Refund reward points to task poster
                    poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    # Create RewardLedger entry
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refund for expired dispute on task: '{task.title}'"
                    )

                    # Create notifications for involved parties
                    link = reverse('dispute_detail', args=[dispute.id])
                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' has expired. Your {task.reward} reward points have been refunded.",
                        link=link
                    )

                    if task.taken_by and task.taken_by != task.posted_by:
                        Notification.objects.create(
                            recipient=task.taken_by,
                            message=f"Dispute for task '{task.title}' has expired and was resolved.",
                            link=link
                        )

                    resolved_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"Resolved expired dispute #{dispute.id} for task '{task.title}'")
                    )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"Error processing dispute #{dispute.id}: {str(e)}")
                )

        self.stdout.write(
            self.style.SUCCESS(f"Finished processing expired disputes. Total resolved: {resolved_count}")
        )
