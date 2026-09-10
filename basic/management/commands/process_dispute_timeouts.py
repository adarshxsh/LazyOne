from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = "Sweep expired disputes and automatically execute escrow settlements."

    def handle(self, *args, **options):
        now = timezone.now()
        expired_disputes = Dispute.objects.filter(
            status='open',
            response_deadline__lt=now
        )

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            raiser = dispute.raised_by
            respondent = task.posted_by if raiser == task.taken_by else task.taken_by

            # Check if respondent submitted counter-evidence or any evidence
            has_respondent_evidence = False
            if respondent:
                has_respondent_evidence = dispute.evidences.filter(submitter=respondent).exists()

            with transaction.atomic():
                if not has_respondent_evidence:
                    # Respondent failed to respond before deadline. Auto-resolve in favor of raiser.
                    if raiser == task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        task.status = 'completed'
                        task.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='dispute_settlement',
                            description=f"Auto-settled dispute timeout in favor of taker for task: '{task.title}'"
                        )

                        dispute.status = 'resolved'
                        dispute.dispute_stage = 'timed_out'
                        dispute.resolved_at = now
                        dispute.resolution_outcome = 'auto_settled_initiator'
                        dispute.save()

                        Notification.objects.create(
                            recipient=task.taken_by,
                            message=f"Dispute for '{task.title}' auto-resolved in your favor due to respondent timeout. {task.reward} points awarded.",
                            link=reverse('dispute_detail', args=[dispute.id])
                        )
                        if task.posted_by:
                            Notification.objects.create(
                                recipient=task.posted_by,
                                message=f"Dispute for '{task.title}' expired without counter-evidence and was auto-resolved in favor of the taker.",
                                link=reverse('dispute_detail', args=[dispute.id])
                            )

                    elif raiser == task.posted_by:
                        poster_profile = task.posted_by.userprofile
                        poster_profile.rewards += task.reward
                        poster_profile.save()

                        task.status = 'cancelled'
                        task.save()

                        RewardLedger.objects.create(
                            user=task.posted_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='dispute_settlement',
                            description=f"Auto-settled dispute timeout refund for task: '{task.title}'"
                        )

                        dispute.status = 'resolved'
                        dispute.dispute_stage = 'timed_out'
                        dispute.resolved_at = now
                        dispute.resolution_outcome = 'auto_settled_initiator'
                        dispute.save()

                        Notification.objects.create(
                            recipient=task.posted_by,
                            message=f"Dispute for '{task.title}' auto-resolved in your favor due to respondent timeout. {task.reward} points refunded.",
                            link=reverse('dispute_detail', args=[dispute.id])
                        )
                        if task.taken_by:
                            Notification.objects.create(
                                recipient=task.taken_by,
                                message=f"Dispute for '{task.title}' expired without response and was auto-resolved in favor of the poster.",
                                link=reverse('dispute_detail', args=[dispute.id])
                            )
                else:
                    if dispute.dispute_stage == 'counter_evidence':
                        dispute.dispute_stage = 'under_review'
                        dispute.save()

            count += 1

        self.stdout.write(self.style.SUCCESS(f"Processed {count} expired dispute(s)."))
