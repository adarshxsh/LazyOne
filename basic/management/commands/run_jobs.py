import time

from django.core.management.base import BaseCommand

from basic.services.jobs import run_due


class Command(BaseCommand):
    help = "Run due jobs (expiry, auto-approve, jury tallies, outbox). Use --loop in development."

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help="Keep running every --interval seconds.")
        parser.add_argument('--interval', type=int, default=30)

    def handle(self, *args, loop=False, interval=30, **opts):
        while True:
            self.stdout.write(str(run_due()))
            if not loop:
                break
            time.sleep(interval)
