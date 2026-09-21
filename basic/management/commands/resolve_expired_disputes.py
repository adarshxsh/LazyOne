from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from basic.models import Dispute
from basic.views.dispute import resolve_dispute_by_consensus


class Command(BaseCommand):
    help = 'Resolves disputes upon quorum (5 votes) or expiration via community consensus majority vote.'

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

        open_disputes = Dispute.objects.filter(status='open')

        count = 0
        for dispute in open_disputes:
            vote_count = dispute.votes.count()

            # Check quorum condition (>= 5 votes)
            if vote_count >= 5:
                resolved = resolve_dispute_by_consensus(dispute)
                if resolved:
                    count += 1
            # Check expiration condition
            elif dispute.created_at <= expiry_threshold:
                # Require at least 3 votes and no tie to resolve expired disputes
                if vote_count < 3:
                    continue

                posted_by_votes = dispute.votes.filter(vote_choice='posted_by').count()
                taken_by_votes = dispute.votes.filter(vote_choice='taken_by').count()

                if posted_by_votes == taken_by_votes:
                    continue

                resolved = resolve_dispute_by_consensus(dispute)
                if resolved:
                    count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} dispute(s)."))
