from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, Notification
from basic.views.dispute import advance_dispute_phase


class Command(BaseCommand):
    help = 'Processes disputes with expired phase time limits and advances them through the FSM lifecycle.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            help='Number of days after phase start before considering it expired (default: 1)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        active_statuses = ['open', 'evidence_phase', 'voting_phase', 'appeal_phase']
        expired_disputes = Dispute.objects.filter(status__in=active_statuses, created_at__lte=expiry_threshold)

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                old_status_display = dispute.get_status_display()
                advance_dispute_phase(dispute)
                new_status_display = dispute.get_status_display()
                count += 1

                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' advanced from {old_status_display} to {new_status_display} due to SLA expiration.",
                        link=dispute_link
                    )

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute phase transition(s)."))
