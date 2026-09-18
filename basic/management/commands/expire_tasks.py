from django.core.management.base import BaseCommand
from basic.views.tasks import expire_overdue_tasks


class Command(BaseCommand):
    help = 'Scans overdue tasks, transitions status to cancelled, and automatically refunds reserved escrow points to task posters.'

    def handle(self, *args, **options):
        result = expire_overdue_tasks()
        expired_count = result.get('expired_count', 0)
        self.stdout.write(
            self.style.SUCCESS(f"Successfully processed {expired_count} expired task(s).")
        )
