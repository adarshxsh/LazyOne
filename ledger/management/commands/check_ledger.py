from django.core.management.base import BaseCommand, CommandError

from ledger.services import check_invariants


class Command(BaseCommand):
    help = "Verify ledger invariants (entries sum to zero, cached balances match). Exit 1 on drift."

    def handle(self, *args, **opts):
        problems = check_invariants()
        if problems:
            for p in problems:
                self.stderr.write(p)
            raise CommandError(f"{len(problems)} ledger problem(s)")
        self.stdout.write(self.style.SUCCESS("Ledger OK"))
