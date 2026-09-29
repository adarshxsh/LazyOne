from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification
from basic.views.dispute import resolve_dispute_with_winner

class Command(BaseCommand):
    help = 'Resolves expired open disputes using jury vote majority, refunds/forfeits escrowed bonds, and settles task points.'

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
            poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
            taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

            if taker_votes > poster_votes and task.taken_by:
                winner = task.taken_by
            elif poster_votes > taker_votes:
                winner = task.posted_by
            else:
                # Fallback if tie or no votes recorded
                if dispute.raised_by == task.posted_by:
                    winner = task.posted_by
                else:
                    winner = task.taken_by or task.posted_by

            resolve_dispute_with_winner(dispute, winner)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
