from django.core.management.base import BaseCommand
from basic.models import Dispute
from basic.services.juror import check_and_replace_expired_jurors


class Command(BaseCommand):
    help = "Unassigns jurors who have not voted within 24 hours and selects replacements."

    def handle(self, *args, **options):
        open_disputes = Dispute.objects.filter(status='open')
        count = 0
        for dispute in open_disputes:
            check_and_replace_expired_jurors(dispute)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} open disputes for expired jurors."))
