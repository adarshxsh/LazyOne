from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired open disputes, refunds symmetrical escrowed bonds, and settles task points.'

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

        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                dispute.status = 'resolved'

                # Execute symmetrical bond refunds for both poster and worker
                dispute.refund_deposit(
                    reason_description=f"Symmetrical deposit bonds refunded on auto-resolved expired dispute for task '{task.title}'"
                )

                # Refund any locked juror stakes
                for vote in dispute.votes.all():
                    juror_profile = vote.juror.userprofile
                    juror_profile.rewards += vote.staked_amount
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=vote.juror,
                        task=task,
                        amount=vote.staked_amount,
                        transaction_type='juror_reward',
                        description=f"Juror stake refunded on expired dispute for task '{task.title}'"
                    )

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
                        description=f"Refund for expired dispute on task: '{task.title}'"
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
                            description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                        )
                    task.status = 'completed'
                    task.save()

                dispute.save()

                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has expired ({days}d SLA) and was automatically resolved with symmetrical bond refunds.",
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
