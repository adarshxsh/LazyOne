from django.core.management.base import BaseCommand
from basic.workers import process_expired_disputes

class Command(BaseCommand):
    help = 'Processes expired disputes, applying auto-resolution rules and releasing escrow points.'

    def handle(self, *args, **options):
        processed_count = process_expired_disputes()
        self.stdout.write(
            self.style.SUCCESS(f"Successfully processed {processed_count} expired dispute(s).")
        )
