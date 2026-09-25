from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired disputes based on active lifecycle phase (evidence submission, peer voting).'

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

        active_disputes = Dispute.objects.filter(
            status__in=['evidence_submission', 'voting', 'open'],
            created_at__lte=expiry_threshold
        )

        processed_count = 0
        for dispute in active_disputes:
            task = dispute.task
            with transaction.atomic():
                if dispute.status == 'evidence_submission':
                    poster_has_ev = dispute.evidence_records.filter(submitted_by=task.posted_by).exists()
                    taker_has_ev = dispute.evidence_records.filter(submitted_by=task.taken_by).exists() if task.taken_by else False

                    if poster_has_ev and not taker_has_ev:
                        dispute.resolve_dispute(
                            winner=task.posted_by,
                            reason_description=f"SLA expired: Auto-resolved in favor of active poster for task '{task.title}'"
                        )
                        processed_count += 1
                    elif taker_has_ev and not poster_has_ev:
                        dispute.resolve_dispute(
                            winner=task.taken_by,
                            reason_description=f"SLA expired: Auto-resolved in favor of active taker for task '{task.title}'"
                        )
                        processed_count += 1
                    else:
                        # Advance to voting phase if both or neither submitted counter-proof
                        dispute.status = 'voting'
                        dispute.save()
                        processed_count += 1

                elif dispute.status == 'voting':
                    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
                    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

                    if poster_votes > taker_votes:
                        winner = task.posted_by
                    elif taker_votes > poster_votes:
                        winner = task.taken_by
                    else:
                        winner = dispute.raised_by

                    dispute.resolve_dispute(
                        winner=winner,
                        reason_description=f"SLA expired in voting phase: Auto-resolved in favor of {winner.username} for task '{task.title}'"
                    )
                    processed_count += 1

                elif dispute.status == 'open':
                    winner = task.posted_by if dispute.raised_by == task.posted_by else task.taken_by
                    dispute.resolve_dispute(
                        winner=winner,
                        reason_description=f"SLA expired: Auto-resolved open dispute for task '{task.title}'"
                    )
                    processed_count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {processed_count} expired dispute(s)."))
