from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired open disputes, processes default wins for missing counter-bonds, refunds/forfeits escrowed bonds, and settles task points.'

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

        count = 0

        # 1. Check open disputes where counter-bond deadline has passed and matching bond was not deposited
        unmatched_disputes = Dispute.objects.filter(
            status='open',
            counter_bond_deadline__lte=now
        )
        for dispute in unmatched_disputes:
            if dispute.poster_deposited and dispute.worker_deposited:
                continue  # Both deposited, proceed with jury or SLA timeout

            task = dispute.task
            with transaction.atomic():
                # Default win for initiator
                initiator = dispute.raised_by
                winner_profile = initiator.userprofile

                # Refund initiator's bond
                dispute.refund_deposit(reason_description=f"Deposit bond refunded on default win for task '{task.title}'")

                winner_profile.refresh_from_db()
                if initiator == task.taken_by:
                    winner_profile.rewards += task.reward
                    winner_profile.save()
                    task.status = 'completed'
                    task.save()

                    RewardLedger.objects.create(
                        user=initiator,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_default_win',
                        description=f"Default win awarded for dispute on task: '{task.title}' due to missing counter-bond"
                    )
                else:
                    winner_profile.rewards += task.reward
                    winner_profile.save()
                    task.status = 'cancelled'
                    task.save()

                    RewardLedger.objects.create(
                        user=initiator,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_default_win',
                        description=f"Default win refund awarded for dispute on task: '{task.title}' due to missing counter-bond"
                    )

                dispute.status = 'resolved'
                dispute.save()

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                Notification.objects.create(
                    recipient=initiator,
                    message=f"You won the dispute for '{task.title}' by default as counter-bond was not deposited within 24 hours.",
                    link=dispute_link
                )
                count += 1

        # 2. Find open disputes created before the expiration window (SLA timeout)
        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                dispute.status = 'resolved'
                dispute.save()

                if dispute.raised_by == task.posted_by:
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

                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has expired ({days}d SLA) and was automatically resolved.",
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
