from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired open disputes, refunds/forfeits escrowed bonds, and settles task points based on evidence submission compliance.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days after dispute creation before considering it expired (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        # Find open disputes with explicit expiration timestamp reached, evidence deadline elapsed, or legacy creation threshold passed
        expired_disputes = Dispute.objects.filter(status='open').filter(
            Q(expires_at__lte=now) | Q(evidence_deadline__lte=now) | Q(created_at__lte=expiry_threshold)
        ).distinct()

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                dispute.status = 'resolved'
                dispute.save()

                poster_has_evidence = dispute.evidences.filter(submitted_by=task.posted_by).exists()
                taker_has_evidence = task.taken_by and dispute.evidences.filter(submitted_by=task.taken_by).exists()

                poster_compliant = (dispute.raised_by == task.posted_by) or poster_has_evidence
                taker_compliant = (dispute.raised_by == task.taken_by) or taker_has_evidence

                if taker_compliant and not poster_compliant:
                    # Taker compliant, poster non-responsive -> award to taker
                    resolve_in_favor_of = 'taker'
                elif poster_compliant and not taker_compliant:
                    # Poster compliant, taker non-responsive -> cancel task / refund poster
                    resolve_in_favor_of = 'poster'
                else:
                    # Both compliant or default -> resolve based on raised_by
                    if dispute.raised_by == task.posted_by:
                        resolve_in_favor_of = 'poster'
                    else:
                        resolve_in_favor_of = 'taker'

                if resolve_in_favor_of == 'poster':
                    # Poster wins: cancel task, refund task reward, refund deposit bond
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    task.status = 'cancelled'
                    task.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refund for expired dispute on task: '{task.title}'"
                    )

                    if dispute.escrow_status == 'held':
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")
                else:
                    # Taker wins: award reward to taker, complete task, refund deposit bond
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                        )
                    task.status = 'completed'
                    task.save()

                    if dispute.escrow_status == 'held':
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                # Notify participants
                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has expired and was automatically resolved.",
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))

