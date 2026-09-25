from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification
from basic.views.dispute import finalize_dispute_resolution


class Command(BaseCommand):
    help = 'Verifies quorum, appeal windows, applies juror penalties, and auto-resolves expired disputes.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=2,
            help='Number of days for voting SLA / appeal window threshold (default: 2 days / 48 hours)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        voting_threshold = now - timedelta(days=days)

        processed_count = 0

        # Phase 1: Open Disputes (Primary Jury Voting)
        open_disputes = Dispute.objects.filter(status='open')
        for dispute in open_disputes:
            task = dispute.task
            with transaction.atomic():
                # First check if primary consensus is already met
                if dispute.check_primary_consensus():
                    self.stdout.write(self.style.SUCCESS(f"Dispute {dispute.id} reached primary consensus."))
                    processed_count += 1
                    continue

                # If voting window (48h) has expired
                if dispute.created_at <= voting_threshold:
                    # Guardrail 3: Jurors who fail to cast a vote within 48 hours lose juror eligibility for 30 days
                    primary_voted_juror_ids = dispute.votes.filter(tier='primary').values_list('juror_id', flat=True)
                    for assigned_juror in dispute.assigned_jurors.all():
                        if assigned_juror.id not in primary_voted_juror_ids:
                            profile = assigned_juror.userprofile
                            profile.juror_ineligible_until = now + timedelta(days=30)
                            profile.save()

                    # Resolve by majority vote if available, or fallback
                    p_votes = dispute.votes.filter(tier='primary')
                    poster_count = p_votes.filter(vote='poster').count()
                    taker_count = p_votes.filter(vote='taker').count()

                    if poster_count > taker_count:
                        winner = 'poster'
                    elif taker_count > poster_count:
                        winner = 'taker'
                    else:
                        # Default fallback: poster if raised by poster, taker if raised by taker
                        winner = 'poster' if dispute.raised_by == task.posted_by else 'taker'

                    finalize_dispute_resolution(dispute, winner, is_appeal_outcome=False)
                    processed_count += 1
                    self.stdout.write(self.style.SUCCESS(f"Dispute {dispute.id} expired without quorum/consensus; auto-resolved for {winner}."))

        # Phase 2: Primary Resolved Disputes (Checking 48-hour Appeal Window)
        primary_resolved_disputes = Dispute.objects.filter(status='primary_resolved')
        for dispute in primary_resolved_disputes:
            if dispute.primary_resolved_at and now >= dispute.primary_resolved_at + timedelta(hours=48):
                if not hasattr(dispute, 'appeal'):
                    # Appeal window expired without appeal -> finalize primary decision
                    with transaction.atomic():
                        winner = dispute.primary_outcome or ('poster' if dispute.raised_by == dispute.task.posted_by else 'taker')
                        finalize_dispute_resolution(dispute, winner, is_appeal_outcome=False)
                        processed_count += 1
                        self.stdout.write(self.style.SUCCESS(f"Dispute {dispute.id} appeal window expired. Finalized primary outcome: {winner}."))

        # Phase 3: Appealed Disputes (Senior Panel Review)
        appealed_disputes = Dispute.objects.filter(status='appealed')
        for dispute in appealed_disputes:
            if dispute.created_at <= voting_threshold or (hasattr(dispute, 'appeal') and dispute.appeal.created_at <= voting_threshold):
                with transaction.atomic():
                    s_votes = dispute.votes.filter(tier='senior')
                    poster_count = s_votes.filter(vote='poster').count()
                    taker_count = s_votes.filter(vote='taker').count()

                    if poster_count > taker_count:
                        winner = 'poster'
                    elif taker_count > poster_count:
                        winner = 'taker'
                    else:
                        winner = dispute.primary_outcome or 'poster'

                    finalize_dispute_resolution(dispute, winner, is_appeal_outcome=True)
                    processed_count += 1
                    self.stdout.write(self.style.SUCCESS(f"Dispute {dispute.id} senior appeal panel review completed/expired; auto-resolved for {winner}."))

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {processed_count} dispute action(s)."))
