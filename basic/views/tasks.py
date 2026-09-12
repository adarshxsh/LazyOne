from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, RewardLedger
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

            user_profile = request.user.userprofile
            if user_profile.rewards < reward:
                messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            deadline = timezone.make_aware(datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M'))
            if deadline <= timezone.now():
                messages.error(request, "Deadline must be in the future.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            with transaction.atomic():
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
    task = get_object_or_404(Task, id=task_id, status='available')
    if task.posted_by == request.user:
        messages.error(request, "You cannot take your own task.")
        return redirect('my_tasks')
    
    collateral_needed = int(task.reward * 0.20)
    user_profile = request.user.userprofile
    if user_profile.rewards < collateral_needed:
        messages.error(
            request,
            f"Insufficient reward points. You need at least {collateral_needed} points as collateral to claim this task."
        )
        return redirect('my_tasks')

    with transaction.atomic():
        user_profile.rewards -= collateral_needed
        user_profile.save()

        task.status = 'in_progress'
        task.taken_by = request.user
        task.collateral_amount = collateral_needed
        task.save()

        RewardLedger.objects.create(
            user=request.user, task=task, amount=-collateral_needed,
            transaction_type='collateral_lock', description=f"Collateral locked for task: '{task.title}'"
        )

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
    task = get_object_or_404(Task, Q(status='in_progress') | Q(status='disputed'), id=task_id, posted_by=request.user)
    with transaction.atomic():
        task_doer_profile = task.taken_by.userprofile
        collateral = task.collateral_amount or 0
        task_doer_profile.rewards += (task.reward + collateral)
        task_doer_profile.save()
        task.status = 'completed'
        task.save()

        if hasattr(task, 'dispute'):
            task.dispute.status = 'resolved'
            task.dispute.save()

        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=task.reward,
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )
        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=collateral,
            transaction_type='collateral_refund', description=f"Collateral refunded for task: '{task.title}'"
        )
        messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def cancel_task(request, task_id):
    task = get_object_or_404(Task, id=task_id, posted_by=request.user, status='available')
    with transaction.atomic():
        task.status = 'cancelled'
        task.save()
        user_profile = request.user.userprofile
        user_profile.rewards += task.reward
        user_profile.save()
        RewardLedger.objects.create(
            user=request.user, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: "
        )
        messages.success(request, "You have cancelled the task and your points have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def request_cancellation(request, task_id):
    task = get_object_or_404(Task, id=task_id, posted_by=request.user, status='in_progress')
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
def accept_cancellation(request, task_id):
    task = get_object_or_404(Task, id=task_id, taken_by=request.user, cancellation_requested=True)
    with transaction.atomic():
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by, task=task, amount=task.reward,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )
        if task.collateral_amount > 0:
            taker_profile = request.user.userprofile
            taker_profile.rewards += task.collateral_amount
            taker_profile.save()
            RewardLedger.objects.create(
                user=request.user, task=task, amount=task.collateral_amount,
                transaction_type='collateral_refund', description=f"Collateral refunded for cancelled task: '{task.title}'"
            )
            task.collateral_amount = 0

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
    task = get_object_or_404(Task, Q(status='in_progress') | Q(status='disputed'), id=task_id, taken_by=request.user)
    with transaction.atomic():
        slashed_collateral = task.collateral_amount or 0
        if slashed_collateral > 0:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += slashed_collateral
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=slashed_collateral,
                transaction_type='collateral_slash', description=f"Collateral compensation from abandoned task: '{task.title}'"
            )
        if hasattr(task, 'dispute'):
            task.dispute.delete()

        task.status = 'available'
        task.taken_by = None
        task.collateral_amount = 0
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
