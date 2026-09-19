from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from basic.models import Dispute
from basic.views.dispute import check_and_expire_dispute

class Command(BaseCommand):
    help = 'Auto-resolves open disputes that have passed their deadline and releases escrowed rewards.'

    def handle(self, *args, **options):
        now = timezone.now()
        stagnant_disputes = list(Dispute.objects.filter(status='open', deadline__lt=now))
        count = 0

        with transaction.atomic():
            for dispute in stagnant_disputes:
                if check_and_expire_dispute(dispute):
                    count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully auto-resolved {count} expired dispute(s)."))
