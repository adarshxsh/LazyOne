from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification


class Command(BaseCommand):
    help = 'Automatically resolves open disputes that have passed their expiration deadline, refunds/forfeits escrowed bonds, and settles task points.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days after dispute creation before considering it expired (default: 7)'
        )

    def handle(self, *args, **options):
        days = options.get('days', 7)
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        expired_disputes = Dispute.objects.filter(status='open').filter(
            Q(expires_at__lte=now) | Q(created_at__lte=expiry_threshold)
        )
        resolved_count = 0

        for dispute in expired_disputes:
            with transaction.atomic():
                # Re-fetch with select_for_update to avoid race conditions
                dispute_obj = Dispute.objects.select_for_update().get(id=dispute.id)
                if dispute_obj.status != 'open':
                    continue

                task = dispute_obj.task
                dispute_obj.status = 'resolved'
                dispute_obj.save()

                if dispute_obj.raised_by == task.posted_by:
                    # Poster challenged an unresponsive taker: cancel task, refund task reward, refund bond
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

                    # Handle escrow bond refund
                    if dispute_obj.escrow_status == 'held':
                        dispute_obj.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                    dispute_link = reverse('dispute_detail', args=[dispute_obj.id])
                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' has expired and was automatically resolved in your favor. {task.reward} points have been refunded to you.",
                        link=dispute_link
                    )
                    if task.taken_by:
                        Notification.objects.create(
                            recipient=task.taken_by,
                            message=f"Dispute for task '{task.title}' has expired and was automatically resolved.",
                            link=dispute_link
                        )
                else:
                    # Taker raised dispute: award reward to taker, complete task, and refund bond
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Auto-resolved expired dispute for task: '{task.title}'"
                        )

                        Notification.objects.create(
                            recipient=task.taken_by,
                            message=f"Dispute for task '{task.title}' has expired and was automatically resolved. {task.reward} points have been credited to you.",
                            link=reverse('dispute_detail', args=[dispute_obj.id])
                        )

                    task.status = 'completed'
                    task.save()

                    if dispute_obj.escrow_status == 'held':
                        dispute_obj.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' has expired and was automatically resolved in favor of the task taker.",
                        link=reverse('dispute_detail', args=[dispute_obj.id])
                    )

                resolved_count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully auto-resolved {resolved_count} expired dispute(s)."))
