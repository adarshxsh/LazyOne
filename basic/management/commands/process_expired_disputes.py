from django.core.management.base import BaseCommand
from basic.views.dispute import process_expired_disputes

class Command(BaseCommand):
    help = 'Evaluates active disputes past their expiration deadline, auto-resolving dormant disputes and executing escrow point transfers.'

    def handle(self, *args, **options):
        count = process_expired_disputes()
        self.stdout.write(self.style.SUCCESS(f'Successfully processed {count} expired dispute(s).'))
