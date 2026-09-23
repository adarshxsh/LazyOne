from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, Notification

class Command(BaseCommand):
    help = 'Processes dispute SLA timeouts across evidence, review, and voting phases, and auto-resolves expired disputes.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days before considering a phase expired (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        active_disputes = Dispute.objects.exclude(status='resolved')
        
        transitioned_count = 0
        resolved_count = 0

        for dispute in active_disputes:
            task = dispute.task
            dispute_updated = dispute.updated_at or dispute.created_at
            
            if dispute_updated > expiry_threshold:
                continue

            with transaction.atomic():
                dispute_link = reverse('dispute_detail', args=[dispute.id])
                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                if dispute.status in ['open', 'evidence_submission']:
                    dispute.transition_to('under_review')
                    transitioned_count += 1
                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Dispute evidence phase for task '{task.title}' has expired ({days}d SLA) and moved to review.",
                            link=dispute_link
                        )

                elif dispute.status == 'under_review':
                    dispute.transition_to('voting')
                    transitioned_count += 1
                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Dispute review phase for task '{task.title}' has expired ({days}d SLA) and moved to jury voting.",
                            link=dispute_link
                        )

                elif dispute.status == 'voting':
                    dispute.resolve_dispute(
                        reason_description=f"Auto-resolved after voting phase expiration ({days}d SLA) on task: '{task.title}'"
                    )
                    resolved_count += 1
                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Dispute for task '{task.title}' voting phase has expired ({days}d SLA) and was automatically resolved.",
                            link=dispute_link
                        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully processed disputes SLA: {transitioned_count} phase transition(s), {resolved_count} resolution(s)."
            )
        )
