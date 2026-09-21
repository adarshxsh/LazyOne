from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from basic.models import Dispute, RewardLedger
from basic.services.dispute import DisputeService

class Command(BaseCommand):
    help = 'Resolves expired open disputes, refunds/forfeits escrowed bonds, and settles task points.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days after dispute creation before considering it expired (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        # Find open disputes created before the expiration window
        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                if dispute.raised_by == task.posted_by:
                    # Poster challenged an unresponsive taker: cancel task, refund task reward, forfeit bond
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
                            description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                        )
                    task.status = 'completed'
                    task.save()

                DisputeService.resolve_dispute(
                    dispute=dispute,
                    actor=None,
                    description=f"Dispute for task '{task.title}' expired ({days}d SLA) and was automatically resolved."
                )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
