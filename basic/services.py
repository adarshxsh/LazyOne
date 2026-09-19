from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from .models import Task, UserProfile, RewardLedger, Notification

def expire_task(task):
    """
    Atomically expires an overdue task in 'available' or 'in_progress' status,
    refunds reward points to the poster, records a RewardLedger entry, and sends notifications.
    Returns True if task was expired, False otherwise.
    """
    now = timezone.now()
    if task.status not in ['available', 'in_progress']:
        return False
    if not task.deadline or task.deadline >= now:
        return False

    with transaction.atomic():
        task_obj = Task.objects.select_for_update().get(pk=task.pk)
        if task_obj.status not in ['available', 'in_progress']:
            return False
        if not task_obj.deadline or task_obj.deadline >= now:
            return False

        task_obj.status = 'expired'
        task_obj.save(update_fields=['status'])

        # Refund poster
        poster_profile, _ = UserProfile.objects.select_for_update().get_or_create(user=task_obj.posted_by)
        poster_profile.rewards += task_obj.reward
        poster_profile.save(update_fields=['rewards'])

        # Record ledger entry
        RewardLedger.objects.create(
            user=task_obj.posted_by,
            task=task_obj,
            amount=task_obj.reward,
            transaction_type='task_expiration',
            description=f"Refund for expired task: '{task_obj.title}'"
        )

        # Notify poster
        Notification.objects.create(
            recipient=task_obj.posted_by,
            message=f"Your task '{task_obj.title}' has expired and your {task_obj.reward} points have been refunded.",
            link=reverse('my_tasks')
        )

        # Notify taker if assigned
        if task_obj.taken_by:
            Notification.objects.create(
                recipient=task_obj.taken_by,
                message=f"The task '{task_obj.title}' you were assigned to has expired.",
                link=reverse('my_tasks')
            )

        task.status = 'expired'
        return True

def expire_overdue_tasks_service():
    """
    Identifies and expires all overdue tasks in 'available' or 'in_progress' status.
    Returns the count of tasks successfully expired.
    """
    now = timezone.now()
    overdue_tasks = Task.objects.filter(
        status__in=['available', 'in_progress'],
        deadline__lt=now
    )
    count = 0
    for task in overdue_tasks:
        if expire_task(task):
            count += 1
    return count
