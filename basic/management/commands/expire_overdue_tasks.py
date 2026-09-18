from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Task, RewardLedger, Notification


class Command(BaseCommand):
    help = "Check task deadlines and refund escrowed points for overdue tasks."

    def handle(self, *args, **options):
        now = timezone.now()
        overdue_tasks = Task.objects.filter(
            deadline__lt=now,
            status__in=['available', 'in_progress']
        ).exclude(dispute__status='open')

        count = 0
        for task in overdue_tasks:
            with transaction.atomic():
                task.status = 'cancelled'
                task.save()

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for expired task: '{task.title}'"
                )

                my_tasks_url = reverse('my_tasks')
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Your task '{task.title}' has expired and {task.reward} points have been refunded.",
                    link=my_tasks_url
                )

                if task.taken_by:
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Task '{task.title}' has expired as the deadline passed.",
                        link=my_tasks_url
                    )

                count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Successfully processed and expired {count} overdue task(s).")
        )
