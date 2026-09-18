from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from basic.models import Dispute, Task, Notification, RewardLedger

class Command(BaseCommand):
    help = 'Automatically expires stale open disputes where the opposing party failed to respond within deadline (default 7 days).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days of inactivity before a dispute expires (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        cutoff_time = timezone.now() - timedelta(days=days)
        open_disputes = Dispute.objects.filter(status='open')

        expired_count = 0

        for dispute in open_disputes:
            task = dispute.task
            
            # Determine the last activity time and who last submitted evidence or raised the dispute
            latest_evidence = dispute.evidences.order_by('-created_at').first()
            if latest_evidence:
                last_activity_time = latest_evidence.created_at
                last_active_user = latest_evidence.uploaded_by
            else:
                last_activity_time = dispute.created_at
                last_active_user = dispute.raised_by

            if last_activity_time <= cutoff_time:
                # The opposing party failed to respond within the deadline
                with transaction.atomic():
                    # Determine unresponsive party and award/refund points accordingly
                    if last_active_user == task.posted_by:
                        # Poster was last active -> Taker (taken_by) is unresponsive/in default
                        # Refund reward points back to poster
                        unresponsive_user = task.taken_by
                        responsive_user = task.posted_by

                        poster_profile = task.posted_by.userprofile
                        poster_profile.rewards += task.reward
                        poster_profile.save()

                        RewardLedger.objects.create(
                            user=task.posted_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_cancellation',
                            description=f"Dispute auto-expired - refunded points for task: '{task.title}'"
                        )
                        task.status = 'cancelled'

                    else:
                        # Taker was last active (or raised dispute) -> Poster (posted_by) is unresponsive/in default
                        # Award reward points to task taker
                        unresponsive_user = task.posted_by
                        responsive_user = task.taken_by

                        if task.taken_by:
                            taker_profile = task.taken_by.userprofile
                            taker_profile.rewards += task.reward
                            taker_profile.save()

                            RewardLedger.objects.create(
                                user=task.taken_by,
                                task=task,
                                amount=task.reward,
                                transaction_type='task_completion',
                                description=f"Dispute auto-expired - awarded reward for task: '{task.title}'"
                            )
                        task.status = 'completed'

                    task.save()
                    dispute.status = 'expired'
                    dispute.save()

                    # Send notifications
                    link = reverse('dispute_detail', args=[dispute.id])
                    
                    if responsive_user:
                        Notification.objects.create(
                            recipient=responsive_user,
                            message=f"The dispute for task '{task.title}' expired due to opposing party's unresponsiveness and was resolved in your favor.",
                            link=link
                        )
                    if unresponsive_user:
                        Notification.objects.create(
                            recipient=unresponsive_user,
                            message=f"The dispute for task '{task.title}' expired due to unresponsiveness within {days} days.",
                            link=link
                        )

                    expired_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Successfully expired dispute ID {dispute.id} for task '{task.title}'."
                        )
                    )

        self.stdout.write(
            self.style.SUCCESS(f"Processed stale disputes. Expired total: {expired_count}")
        )
