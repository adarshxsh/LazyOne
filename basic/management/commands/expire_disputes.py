from django.core.management.base import BaseCommand
from django.utils import timezone
from basic.models import Dispute
from basic.views.dispute import process_dispute_expiration

class Command(BaseCommand):
    help = 'Identifies expired open disputes and executes automated timeout resolution.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired_disputes = Dispute.objects.filter(
            status='open',
            evidence_deadline__lt=now
        )
        total_found = expired_disputes.count()
        resolved_count = 0

        for dispute in expired_disputes:
            if process_dispute_expiration(dispute):
                resolved_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully processed {total_found} expired dispute(s); resolved {resolved_count}."
            )
        )
