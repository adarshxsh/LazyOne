from django.core.management.base import BaseCommand
from basic.services import expire_overdue_tasks_service

class Command(BaseCommand):
    help = 'Expires overdue tasks and refunds escrowed reward points to task posters.'

    def handle(self, *args, **options):
        count = expire_overdue_tasks_service()
        self.stdout.write(self.style.SUCCESS(f'Successfully expired {count} overdue task(s).'))
