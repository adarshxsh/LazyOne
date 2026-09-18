from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, Task, RewardLedger, Notification, UserProfile

class Command(BaseCommand):
    help = 'Process expired open disputes, auto-resolve them based on evidence/fallback rules, refund or award points, and notify parties.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired_disputes = Dispute.objects.filter(status='open', expires_at__lt=now)
        count = 0

        for dispute in expired_disputes:
            with transaction.atomic():
                task = dispute.task
                poster = task.posted_by
                worker = task.taken_by

                poster_evidence_count = dispute.evidence.filter(user=poster).count() if poster else 0
                worker_evidence_count = dispute.evidence.filter(user=worker).count() if worker else 0

                # Determine fallback resolution rule:
                # If worker provided evidence but poster did not -> Resolve in favor of worker (Complete task)
                if worker and worker_evidence_count > 0 and poster_evidence_count == 0:
                    task.status = 'completed'
                    task.save()

                    if hasattr(worker, 'userprofile'):
                        worker_profile = worker.userprofile
                    else:
                        worker_profile, _ = UserProfile.objects.get_or_create(user=worker)
                    
                    worker_profile.rewards += task.reward
                    worker_profile.save()

                    RewardLedger.objects.create(
                        user=worker,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Auto-resolved dispute payout for task: '{task.title}'"
                    )

                    dispute.status = 'resolved'
                    dispute.resolved_at = timezone.now()
                    dispute.resolution_reason = "Auto-resolved upon expiration in favor of worker (worker provided evidence)."
                    dispute.save()

                    Notification.objects.create(
                        recipient=worker,
                        message=f"Dispute for task '{task.title}' expired and was resolved in your favor. {task.reward} points awarded.",
                        link=reverse('my_tasks')
                    )
                    if poster:
                        Notification.objects.create(
                            recipient=poster,
                            message=f"Dispute for task '{task.title}' expired and was resolved in favor of worker.",
                            link=reverse('my_tasks')
                        )

                else:
                    # Otherwise (poster provided evidence & worker didn't, OR neither, OR both) -> Resolve by cancelling task & refunding poster
                    task.status = 'cancelled'
                    task.save()

                    if hasattr(poster, 'userprofile'):
                        poster_profile = poster.userprofile
                    else:
                        poster_profile, _ = UserProfile.objects.get_or_create(user=poster)

                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=poster,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refund for expired disputed task: '{task.title}'"
                    )

                    dispute.status = 'resolved'
                    dispute.resolved_at = timezone.now()
                    dispute.resolution_reason = "Auto-resolved upon expiration (escrowed points refunded to task poster)."
                    dispute.save()

                    Notification.objects.create(
                        recipient=poster,
                        message=f"Dispute for task '{task.title}' expired and was resolved. {task.reward} points refunded.",
                        link=reverse('my_tasks')
                    )
                    if worker:
                        Notification.objects.create(
                            recipient=worker,
                            message=f"Dispute for task '{task.title}' expired and was closed. Task cancelled.",
                            link=reverse('my_tasks')
                        )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
