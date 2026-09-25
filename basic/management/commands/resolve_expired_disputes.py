from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from basic.models import Dispute
from basic.services import DisputeLifecycleService

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
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        # Find open disputes created before the expiration window
        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        count = 0
        for dispute in expired_disputes:
            DisputeLifecycleService.resolve_expired_dispute(dispute=dispute, days=days)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
