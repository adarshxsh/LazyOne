from django.core.management.base import BaseCommand
from basic.services.sla_engine import SLATimerEngine

class Command(BaseCommand):
    help = 'Executes periodic multi-phase SLA timer checks and automated dispute state transitions.'

    def handle(self, *args, **options):
        counts = SLATimerEngine.process_dispute_sla_transitions()
        self.stdout.write(
            self.style.SUCCESS(
                f"SLA Timer Engine execution complete. Processed {counts['total']} transition(s): "
                f"Evidence->Voting: {counts['evidence_to_voting']}, "
                f"Voting->Appeal: {counts['voting_to_appeal']}, "
                f"Appeal->Resolved: {counts['appeal_to_resolved']}."
            )
        )
