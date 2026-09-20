from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification
from basic.views.dispute import settle_dispute_voting_outcome

class Command(BaseCommand):
    help = 'Resolves expired disputes (poster counter-bond timeouts and voting SLA expirations).'

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

        count = 0

        # 1. Process open disputes awaiting counter-bond that have expired
        expired_open_disputes = Dispute.objects.filter(status='open')
        for dispute in expired_open_disputes:
            if (dispute.counter_bond_deadline and dispute.counter_bond_deadline <= now) or (dispute.created_at <= expiry_threshold):
                task = dispute.task
                with transaction.atomic():
                    # Worker wins by default because poster failed to post matching counter-bond
                    dispute.consensus_outcome = 'worker_wins'
                    dispute.status = 'resolved'
                    dispute.save()

                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward + dispute.worker_deposit_amount
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                        )
                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=dispute.worker_deposit_amount,
                            transaction_type='dispute_refund',
                            description=f"Security deposit bond refunded on auto-resolved dispute for task: '{task.title}'"
                        )
                        dispute.worker_escrow_status = 'refunded'
                        dispute.save()

                    task.status = 'completed'
                    task.save()

                    dispute_link = reverse('dispute_detail', args=[dispute.id])
                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' expired without counter-bond and was resolved in favor of the worker.",
                        link=dispute_link
                    )
                    if task.taken_by:
                        Notification.objects.create(
                            recipient=task.taken_by,
                            message=f"Dispute for task '{task.title}' expired without counter-bond and was resolved in your favor.",
                            link=dispute_link
                        )
                    count += 1

        # 2. Process voting disputes whose voting deadline or SLA threshold has expired
        expired_voting_disputes = Dispute.objects.filter(status='voting')
        for dispute in expired_voting_disputes:
            if (dispute.voting_deadline and dispute.voting_deadline <= now) or (dispute.created_at <= expiry_threshold):
                settle_dispute_voting_outcome(dispute)
                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
