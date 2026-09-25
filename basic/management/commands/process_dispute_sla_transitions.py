from django.core.management.base import BaseCommand
from basic.management.commands.resolve_expired_disputes import Command as ResolveExpiredCommand

class Command(BaseCommand):
    help = 'Processes dispute SLA transitions and resolves expired disputes with symmetrical bond refunds.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days after dispute creation before considering it expired (default: 7)'
        )

    def handle(self, *args, **options):
        cmd = ResolveExpiredCommand()
        cmd.stdout = self.stdout
        cmd.stderr = self.stderr
        cmd.handle(*args, **options)
