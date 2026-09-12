from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, RewardLedger
from ..services.reputation import ReputationService
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
            multiplier = ReputationService.get_poster_collateral_multiplier(user_profile.risk_tier)
            required_collateral = int(reward * multiplier)

            if user_profile.rewards < required_collateral:
                messages.error(request, f"You need {required_collateral} points to post this task (including risk collateral factor), but you only have {user_profile.rewards} points.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            deadline = timezone.make_aware(datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M'))
            if deadline <= timezone.now():
                messages.error(request, "Deadline must be in the future.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            with transaction.atomic():
                user_profile.rewards -= required_collateral
                user_profile.save()
                new_task = Task.objects.create(
                    title=title, description=description, reward=reward,
                    poster_collateral=required_collateral,
                    posted_by=request.user, deadline=deadline, status='available'
                )
                RewardLedger.objects.create(
                    user=request.user, task=new_task, amount=-required_collateral,
                    transaction_type='task_creation', description=f"Reserved for task: '{title}' ({required_collateral} points)"
                )
            messages.success(request, f'Task added successfully! {required_collateral} points have been reserved.')
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

    taker_profile = request.user.userprofile
    can_claim, err_msg = ReputationService.can_claim_task(taker_profile, task.reward)
    if not can_claim:
        messages.error(request, err_msg)
        return redirect('my_tasks')

    taker_collateral = ReputationService.get_taker_required_collateral(taker_profile, task.reward)
    if taker_profile.rewards < taker_collateral:
        messages.error(request, f"Claiming this task requires a security deposit of {taker_collateral} points for your risk level, but you only have {taker_profile.rewards} points.")
        return redirect('my_tasks')

    with transaction.atomic():
        if taker_collateral > 0:
            taker_profile.rewards -= taker_collateral
            taker_profile.save()
            RewardLedger.objects.create(
                user=request.user, task=task, amount=-taker_collateral,
                transaction_type='collateral_lock', description=f"Security deposit locked for task: '{task.title}'"
            )

        task.status = 'in_progress'
        task.taken_by = request.user
        task.taker_collateral = taker_collateral
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
    task = get_object_or_404(Task, Q(status='in_progress') | Q(status='disputed'), id=task_id, posted_by=request.user)
    with transaction.atomic():
        task_doer_profile = task.taken_by.userprofile
        total_payout = task.reward + task.taker_collateral
        task_doer_profile.rewards += total_payout
        task_doer_profile.save()
        task.status = 'completed'
        task.save()

        poster_profile = request.user.userprofile
        extra_poster_collateral = task.poster_collateral - task.reward
        if extra_poster_collateral > 0:
            poster_profile.rewards += extra_poster_collateral
            poster_profile.save()
            RewardLedger.objects.create(
                user=request.user, task=task, amount=extra_poster_collateral,
                transaction_type='collateral_refund', description=f"Extra collateral refunded for task: '{task.title}'"
            )

        if hasattr(task, 'dispute'):
            task.dispute.status = 'resolved'
            task.dispute.save()
            ReputationService.record_dispute_won(task_doer_profile)
            ReputationService.record_dispute_lost(poster_profile)
        else:
            ReputationService.record_task_completion(task_doer_profile)

        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=task.reward,
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )
        if task.taker_collateral > 0:
            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.taker_collateral,
                transaction_type='collateral_refund', description=f"Security deposit returned for task: '{task.title}'"
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
        refund_amount = task.poster_collateral if task.poster_collateral > 0 else task.reward
        user_profile.rewards += refund_amount
        user_profile.save()
        RewardLedger.objects.create(
            user=request.user, task=task, amount=refund_amount,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
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
        refund_amount = task.poster_collateral if task.poster_collateral > 0 else task.reward
        poster_profile.rewards += refund_amount
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by, task=task, amount=refund_amount,
            transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
        )

        if task.taker_collateral > 0:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.taker_collateral
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.taker_collateral,
                transaction_type='collateral_refund', description=f"Security deposit refunded for cancelled task: '{task.title}'"
            )

        task.status = 'available'
        task.taken_by = None
        task.taker_collateral = 0
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
    task = get_object_or_404(Task, id=task_id, taken_by=request.user, status='in_progress')
    with transaction.atomic():
        doer_profile = request.user.userprofile
        if task.taker_collateral > 0:
            RewardLedger.objects.create(
                user=request.user, task=task, amount=-task.taker_collateral,
                transaction_type='collateral_forfeit', description=f"Security deposit forfeited for abandoning task: '{task.title}'"
            )
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.taker_collateral
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=task.taker_collateral,
                transaction_type='collateral_refund', description=f"Received forfeited security deposit from abandoner for task: '{task.title}'"
            )

        ReputationService.record_task_abandonment(doer_profile)

        task.status = 'available'
        task.taken_by = None
        task.taker_collateral = 0
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
