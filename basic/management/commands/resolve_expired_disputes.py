from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

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
        now = timezone.now()

        # Find open disputes where voting_deadline has passed
        expired_disputes = Dispute.objects.filter(status='open', voting_deadline__lte=now)

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                dispute.status = 'resolved'
                dispute.save()

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

                    # Handle escrow bond refund / forfeiture
                    if dispute.escrow_status == 'held':
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")
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

                    if dispute.escrow_status == 'held':
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                # Notify participants
                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has expired and was automatically resolved.",
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
