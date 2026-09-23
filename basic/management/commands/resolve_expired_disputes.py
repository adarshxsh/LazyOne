from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification, User

class Command(BaseCommand):
    help = 'Processes expired open disputes: settles if majority consensus exists, or falls back to staff resolution.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=48,
            help='Number of hours after dispute creation before considering it expired (default: 48)'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help='Number of days after dispute creation before considering it expired (overrides --hours if set)'
        )

    def handle(self, *args, **options):
        if options['days'] is not None:
            hours = options['days'] * 24
        else:
            hours = options['hours']

        now = timezone.now()
        expiry_threshold = now - timedelta(hours=hours)

        # Find open disputes created before the expiration window
        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        count = 0
        consensus_count = 0
        staff_review_count = 0

        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                # 1. Check if consensus exists among submitted votes
                if dispute.check_consensus_and_settle():
                    consensus_count += 1
                else:
                    # 2. Fallback to staff review
                    dispute.status = 'staff_review'
                    dispute.save()
                    staff_review_count += 1

                    # Notify task participants
                    participants = [task.posted_by]
                    if task.taken_by and task.taken_by not in participants:
                        participants.append(task.taken_by)

                    dispute_link = reverse('dispute_detail', args=[dispute.id])
                    for participant in participants:
                        Notification.objects.create(
                            recipient=participant,
                            message=f"Dispute for task '{task.title}' reached the 48-hour deadline without majority consensus and has been escalated to staff review.",
                            link=dispute_link
                        )

                    # Notify staff users
                    staff_users = User.objects.filter(is_staff=True, is_active=True)
                    for staff_user in staff_users:
                        Notification.objects.create(
                            recipient=staff_user,
                            message=f"Dispute #{dispute.id} for task '{task.title}' requires staff resolution.",
                            link=dispute_link
                        )

                count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Successfully processed {count} expired dispute(s) ({consensus_count} settled by consensus, {staff_review_count} escalated to staff review)."
        ))

