from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Task, RewardLedger, Notification


class Command(BaseCommand):
    help = 'Enforces task deadlines by cancelling overdue tasks and refunding escrowed reward points to posters.'

    def handle(self, *args, **options):
        now = timezone.now()
        overdue_tasks = Task.objects.filter(
            deadline__isnull=False,
            deadline__lte=now,
            status__in=['available', 'in_progress']
        )

        count = 0
        dashboard_link = reverse('my_tasks')

        for task in overdue_tasks:
            with transaction.atomic():
                is_in_progress = (task.status == 'in_progress')
                poster = task.posted_by
                taker = task.taken_by
                reward = task.reward

                task.status = 'cancelled'
                task.save()

                poster_profile = poster.userprofile
                poster_profile.rewards += reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for expired task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=poster,
                    message=f"Your task '{task.title}' passed its deadline and was automatically cancelled. Reward points have been refunded.",
                    link=dashboard_link
                )

                if is_in_progress and taker:
                    Notification.objects.create(
                        recipient=taker,
                        message=f"The task '{task.title}' was automatically cancelled because the deadline has passed.",
                        link=dashboard_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} overdue task(s)."))
