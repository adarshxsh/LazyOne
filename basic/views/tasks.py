from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, RewardLedger, Dispute, UserProfile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.db.models import Q, F
from django.views.decorators.http import require_POST
from datetime import datetime, timedelta


class _InsufficientPoints(Exception):
    def __init__(self, balance):
        self.balance = balance

@login_required(login_url='/login/')
def add_task(request):
    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        reward_str = request.POST.get('reward')
        deadline_str = request.POST.get('deadline') # Expecting YYYY-MM-DDTHH:MM format

        try:
            reward = int(reward_str)
            if reward <= 0:
                messages.error(request, "Reward must be a positive number.")
                # Recalculate default_deadline for re-rendering the form
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            user_profile = request.user.userprofile
            if user_profile.rewards < reward:
                messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                # Recalculate default_deadline for re-rendering the form
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            deadline = timezone.make_aware(datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M'))
            if deadline <= timezone.now():
                messages.error(request, "Deadline must be in the future.")
                # Recalculate default_deadline for re-rendering the form
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            with transaction.atomic():
                # Re-check the balance under a row lock so concurrent posts cannot overdraw.
                locked_profile = UserProfile.objects.select_for_update().get(pk=user_profile.pk)
                if locked_profile.rewards < reward:
                    raise _InsufficientPoints(locked_profile.rewards)
                UserProfile.objects.filter(pk=user_profile.pk).update(rewards=F('rewards') - reward)
                new_task = Task.objects.create(
                    title=title, description=description, reward=reward,
                    posted_by=request.user, deadline=deadline, status='available'
                )
                RewardLedger.objects.create(
                    user=request.user, task=new_task, amount=-reward,
                    transaction_type='task_creation', description=f"Reserved for task: '{title}'"
                )
            messages.success(request, f'Task added successfully! {reward} points have been reserved.')
            return redirect('home')
        except _InsufficientPoints as e:
            messages.error(request, f"You only have {e.balance} points, not enough to offer this reward.")
            default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
            return render(request, 'add_task.html', {'default_deadline': default_deadline})
        except (ValueError, TypeError):
            messages.error(request, 'Invalid reward amount or deadline format.')
            # Recalculate default_deadline for re-rendering the form
            default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
            return render(request, 'add_task.html', {'default_deadline': default_deadline})

    else:
        # For GET request, set the default deadline to 1 day from now
        default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        return render(request, 'add_task.html', {'default_deadline': default_deadline})

def _lock_task(**filters):
    """Fetch a task with a row lock inside the current transaction, or None."""
    return Task.objects.select_for_update().filter(**filters).first()


@login_required(login_url='/login/')
@require_POST
def take_task(request, public_id):
    with transaction.atomic():
        task = _lock_task(public_id=public_id, status=Task.Status.AVAILABLE)
        if task is None:
            messages.error(request, "Task is no longer available.")
            return redirect('my_tasks')

        if task.posted_by_id == request.user.id:
            messages.error(request, "You cannot take your own task.")
            return redirect('my_tasks')

        task.status = Task.Status.IN_PROGRESS
        task.taken_by = request.user
        task.save()
        conversation, _ = Conversation.objects.get_or_create(task=task)
        # Re-taken tasks reuse the conversation; make sure the new taker is a participant.
        conversation.participants.add(task.posted_by, task.taken_by)
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has taken your task: {task.title}",
            link=reverse('my_tasks')
        )
    messages.success(request, "Task has been assigned to you. A chat has been created.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def complete_task(request, public_id):
    # Allow completion if the task is in progress OR disputed (poster concedes).
    with transaction.atomic():
        task = _lock_task(
            public_id=public_id,
            posted_by=request.user,
            status__in=[Task.Status.IN_PROGRESS, Task.Status.DISPUTED],
        )
        if task is None or task.taken_by_id is None:
            messages.error(request, "This task cannot be completed.")
            return redirect('my_tasks')

        UserProfile.objects.filter(user_id=task.taken_by_id).update(rewards=F('rewards') + task.reward)
        task.status = Task.Status.COMPLETED
        task.save()

        dispute = Dispute.objects.select_for_update().filter(task=task, status=Dispute.Status.OPEN).first()
        if dispute:
            dispute.status = Dispute.Status.RESOLVED
            dispute.save()

        RewardLedger.objects.create(
            user_id=task.taken_by_id, task=task, amount=task.reward,
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )
    messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def cancel_task(request, public_id):
    with transaction.atomic():
        task = _lock_task(public_id=public_id, posted_by=request.user, status=Task.Status.AVAILABLE)
        if task is None:
            messages.error(request, "This task cannot be cancelled.")
            return redirect('my_tasks')
        task.status = Task.Status.CANCELLED
        task.save()
        UserProfile.objects.filter(user=request.user).update(rewards=F('rewards') + task.reward)
        RewardLedger.objects.create(
            user=request.user, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )
    messages.success(request, "You have cancelled the task and your points have been refunded.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def request_cancellation(request, public_id):
    with transaction.atomic():
        task = _lock_task(public_id=public_id, posted_by=request.user, status=Task.Status.IN_PROGRESS)
        if task is None:
            messages.error(request, "Cancellation can only be requested for a task in progress.")
            return redirect('my_tasks')
        task.cancellation_requested = True
        task.save()
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"{request.user.username} has requested to cancel the task: '{task.title}'.",
            link=reverse('my_tasks')
        )
    messages.success(request, "A cancellation request has been sent to the task taker.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def accept_cancellation(request, public_id):
    """
    Taker agrees to the poster's cancellation request.
    The task is moved to the terminal CANCELLED state and the poster is refunded.
    (Previously the task was re-opened *and* refunded, which let the next taker
    be paid with points that no longer existed.)
    """
    with transaction.atomic():
        task = _lock_task(
            public_id=public_id, taken_by=request.user,
            cancellation_requested=True, status=Task.Status.IN_PROGRESS,
        )
        if task is None:
            messages.error(request, "There is no pending cancellation for this task.")
            return redirect('my_tasks')
        UserProfile.objects.filter(user_id=task.posted_by_id).update(rewards=F('rewards') + task.reward)
        RewardLedger.objects.create(
            user_id=task.posted_by_id, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )
        task.status = Task.Status.CANCELLED
        task.cancellation_requested = False
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} accepted your cancellation request for '{task.title}'. Your points have been refunded.",
            link=reverse('my_tasks')
        )
    messages.success(request, "You have accepted the cancellation.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def abandon_task(request, public_id):
    with transaction.atomic():
        task = _lock_task(public_id=public_id, taken_by=request.user, status=Task.Status.IN_PROGRESS)
        if task is None:
            messages.error(request, "This task cannot be abandoned.")
            return redirect('my_tasks')
        task.status = Task.Status.AVAILABLE
        task.taken_by = None
        task.cancellation_requested = False
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has abandoned your task: '{task.title}'. It is now available again.",
            link=reverse('my_tasks')
        )
    messages.success(request, "You have abandoned the task. It is now available for others.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
def raise_dispute(request, public_id):
    task = get_object_or_404(Task, public_id=public_id)

    # If an active dispute already exists, just go to the detail page.
    existing = Dispute.objects.filter(task=task, status=Dispute.Status.OPEN).first()
    if existing:
        return redirect('dispute_detail', public_id=existing.public_id)

    # GET: the modal is handled client-side.
    if request.method != 'POST':
        return redirect('my_tasks')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        messages.error(request, "A reason is required to raise a dispute.")
        return redirect('my_tasks')

    with transaction.atomic():
        task = _lock_task(pk=task.pk)
        # Re-check participation and state under the lock.
        if request.user.id not in (task.posted_by_id, task.taken_by_id) or task.status != Task.Status.IN_PROGRESS:
            messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
            return redirect('my_tasks')

        dispute = Dispute.objects.select_for_update().filter(task=task).first()
        if dispute:
            # Only a previously withdrawn dispute can be re-raised (resolved ones end the task).
            dispute.raised_by = request.user
            dispute.reason = reason
            dispute.status = Dispute.Status.OPEN
            dispute.save()
        else:
            dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)

        task.status = Task.Status.DISPUTED
        task.save()
        other_user = task.posted_by if request.user.id == task.taken_by_id else task.taken_by
        Notification.objects.create(
            recipient=other_user,
            message=f"{request.user.username} has raised a dispute for the task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.public_id])
        )
    messages.success(request, "Dispute raised successfully.")
    return redirect('dispute_detail', public_id=dispute.public_id)


@login_required(login_url='/login/')
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    context = {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks}
    return render(request, 'my_tasks.html', context)
