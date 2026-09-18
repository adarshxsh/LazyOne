from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification


class Command(BaseCommand):
    help = 'Automatically resolves open disputes that have passed their expiration deadline.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired_disputes = Dispute.objects.filter(status='open', expires_at__lte=now)
        resolved_count = 0

        for dispute in expired_disputes:
            with transaction.atomic():
                # Re-fetch with select_for_update to avoid race conditions
                dispute_obj = Dispute.objects.select_for_update().get(id=dispute.id)
                if dispute_obj.status != 'open':
                    continue

                task = dispute_obj.task
                dispute_obj.status = 'resolved'
                dispute_obj.save()

                task.status = 'completed'
                task.save()

                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Auto-resolved expired dispute for task: '{task.title}'"
                    )

                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Dispute for task '{task.title}' has expired and was automatically resolved. {task.reward} points have been credited to you.",
                        link=reverse('dispute_detail', args=[dispute_obj.id])
                    )

                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' has expired and was automatically resolved in favor of the task taker.",
                    link=reverse('dispute_detail', args=[dispute_obj.id])
                )

                resolved_count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully auto-resolved {resolved_count} expired dispute(s)."))
