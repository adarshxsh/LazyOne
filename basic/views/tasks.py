from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, RewardLedger, UserProfile, Dispute
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.db.models import Q
from datetime import datetime, timedelta


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
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            deadline = timezone.make_aware(datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M'))
            if deadline <= timezone.now():
                messages.error(request, "Deadline must be in the future.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            with transaction.atomic():
                user_profile = UserProfile.objects.select_for_update().get(user=request.user)
                if user_profile.rewards < reward:
                    messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                    default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                    return render(request, 'add_task.html', {'default_deadline': default_deadline})

                user_profile.rewards -= reward
                user_profile.save()
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
        except (ValueError, TypeError):
            messages.error(request, 'Invalid reward amount or deadline format.')
            default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
            return render(request, 'add_task.html', {'default_deadline': default_deadline})

    else:
        default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        return render(request, 'add_task.html', {'default_deadline': default_deadline})

@login_required(login_url='/login/')
def take_task(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.status != 'available':
            messages.error(request, "Task is no longer available.")
            return redirect('my_tasks')

        if task.posted_by == request.user:
            messages.error(request, "You cannot take your own task.")
            return redirect('my_tasks')

        task.status = 'in_progress'
        task.taken_by = request.user
        task.save()
        conversation, created = Conversation.objects.get_or_create(task=task)
        if created:
            conversation.participants.add(task.posted_by, task.taken_by)
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has taken your task: {task.title}",
            link=reverse('my_tasks')
        )
        messages.success(request, "Task has been assigned to you. A chat has been created.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def complete_task(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.posted_by != request.user:
            messages.error(request, "You are not authorized to complete this task.")
            return redirect('my_tasks')

        if task.status not in ('in_progress', 'disputed'):
            messages.error(request, f"Task cannot be completed because its current status is '{task.status}'.")
            return redirect('my_tasks')

        if not task.taken_by:
            messages.error(request, "Task cannot be completed because no user has taken it.")
            return redirect('my_tasks')

        task_doer_profile = UserProfile.objects.select_for_update().get(user=task.taken_by)
        task_doer_profile.rewards += task.reward
        task_doer_profile.save()

        task.status = 'completed'
        task.cancellation_requested = False
        task.save()

        if hasattr(task, 'dispute') and task.dispute.status == 'open':
            dispute = Dispute.objects.select_for_update().get(id=task.dispute.id)
            if dispute.status == 'open':
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'"
                )
                dispute.status = 'resolved'
                dispute.save()

        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=task.reward,
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )
        messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def cancel_task(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.posted_by != request.user:
            messages.error(request, "You are not authorized to cancel this task.")
            return redirect('my_tasks')

        if task.status != 'available':
            messages.error(request, "Task cannot be cancelled as it is no longer available.")
            return redirect('my_tasks')

        task.status = 'cancelled'
        task.save()
        user_profile = UserProfile.objects.select_for_update().get(user=request.user)
        user_profile.rewards += task.reward
        user_profile.save()
        RewardLedger.objects.create(
            user=request.user, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )
        messages.success(request, "You have cancelled the task and your points have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def request_cancellation(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.posted_by != request.user:
            messages.error(request, "You are not authorized to request cancellation for this task.")
            return redirect('my_tasks')

        if task.status != 'in_progress':
            messages.error(request, "Cancellation can only be requested for tasks in progress.")
            return redirect('my_tasks')

        if task.cancellation_requested:
            messages.error(request, "Cancellation has already been requested for this task.")
            return redirect('my_tasks')

        task.cancellation_requested = True
        task.save()
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"{request.user.username} has requested to cancel the task: '{task.title}'.",
                link=reverse('my_tasks')
            )
        messages.success(request, "A cancellation request has been sent to the task taker.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def accept_cancellation(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.taken_by != request.user:
            messages.error(request, "You are not authorized to accept cancellation for this task.")
            return redirect('my_tasks')

        if not task.cancellation_requested:
            messages.error(request, "Cancellation has not been requested for this task.")
            return redirect('my_tasks')

        if task.status != 'in_progress':
            messages.error(request, f"Cannot accept cancellation for a task with status '{task.status}'.")
            return redirect('my_tasks')

        poster_profile = UserProfile.objects.select_for_update().get(user=task.posted_by)
        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )
        task.status = 'available'
        task.taken_by = None
        task.cancellation_requested = False
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} accepted your cancellation request for '{task.title}'. The task is now available again.",
            link=reverse('my_tasks')
        )
        messages.success(request, "You have accepted the cancellation. The task is now available for others.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def abandon_task(request, task_id):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if task.taken_by != request.user:
            messages.error(request, "You are not authorized to abandon this task.")
            return redirect('my_tasks')

        if task.status != 'in_progress':
            messages.error(request, "Task cannot be abandoned because it is not in progress.")
            return redirect('my_tasks')

        task.status = 'available'
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
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    context = {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks}
    return render(request, 'my_tasks.html', context)
