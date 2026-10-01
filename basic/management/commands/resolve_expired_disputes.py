from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from basic.models import Dispute
from basic.views.dispute import resolve_dispute_instance

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

        open_disputes = Dispute.objects.filter(status='open')

        count = 0
        for dispute in open_disputes:
            if not dispute.has_counter_deposit and dispute.is_counter_bond_expired:
                resolve_dispute_instance(dispute)
                count += 1
            elif dispute.has_counter_deposit and dispute.created_at <= expiry_threshold:
                resolve_dispute_instance(dispute)
                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
