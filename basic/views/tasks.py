from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from ..models import Task, Conversation, Notification, RewardLedger, UserProfile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.db.models import Q
from django.conf import settings
from datetime import datetime, timedelta
import math
from django.http import HttpRequest


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
    else:
        collateral_percentage = getattr(settings, 'TASK_COLLATERAL_PERCENTAGE', 20)
        collateral_amount = max(20, math.floor(task.reward * collateral_percentage / 100))
        user_profile = request.user.userprofile
        if user_profile.rewards < collateral_amount:
            messages.error(request, f"You need at least {collateral_amount} points as collateral to take this task.")
            return redirect('my_tasks')

        with transaction.atomic():
            user_profile.rewards -= collateral_amount
            user_profile.save()

            task.status = 'in_progress'
            task.taken_by = request.user
            task.taker_collateral = collateral_amount
            task.save()

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-collateral_amount,
                transaction_type='collateral_lock',
                description=f"Collateral locked for task: '{task.title}'"
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
        task_doer_profile.rewards += task.reward

        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=task.reward,
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )

        if task.taker_collateral > 0:
            task_doer_profile.rewards += task.taker_collateral
            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.taker_collateral,
                transaction_type='collateral_release', description=f"Collateral released for completed task: '{task.title}'"
            )

        task_doer_profile.save()
        task.status = 'completed'
        task.save()

        if hasattr(task, 'dispute') and task.dispute.status == 'open':
            task.dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'"
            )
            task.dispute.status = 'resolved'
            task.dispute.save()

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

        if task.taker_collateral > 0:
            taker_profile = request.user.userprofile
            taker_profile.rewards += task.taker_collateral
            taker_profile.save()
            RewardLedger.objects.create(
                user=request.user, task=task, amount=task.taker_collateral,
                transaction_type='collateral_release', description=f"Collateral released for cancelled task: '{task.title}'"
            )
            task.taker_collateral = 0

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
    task = get_object_or_404(Task, id=task_id, taken_by=request.user, status='in_progress')
    with transaction.atomic():
        if task.taker_collateral > 0:
            taker_profile = request.user.userprofile
            taker_profile.rewards += task.taker_collateral
            taker_profile.save()
            RewardLedger.objects.create(
                user=request.user, task=task, amount=task.taker_collateral,
                transaction_type='collateral_release', description=f"Collateral released on task abandonment: '{task.title}'"
            )
            task.taker_collateral = 0
        task.status = 'available'
        task.taken_by = None
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has abandoned your task: '{task.title}'. It is now available again.",
            link=reverse('my_tasks')
        )
        messages.success(request, "You have abandoned the task. It is now available for others.")
    return redirect('my_tasks')

def slash_user(user, harmed_poster=None, reason="Fraudulent activity detected"):
    """
    Confiscates reward balance and active staked collateral from a fraudulent user.
    Flags account as fraudulent (userprofile.is_fraudulent = True).
    Slashed collateral points are optionally credited to harmed poster.
    Can also be called as a Django view handler: slash_user(request, user_id).
    """
    if isinstance(user, HttpRequest):
        request = user
        user_id = harmed_poster
        if not request.user.is_staff:
            messages.error(request, "Permission denied.")
            return redirect('home')
        target_user = get_object_or_404(User, id=user_id)
        req_reason = request.POST.get('reason', 'Fraudulent activity detected') if request.method == 'POST' else 'Fraudulent activity detected'
        slash_user(target_user, reason=req_reason)
        messages.success(request, f"User {target_user.username} has been slashed for fraud.")
        return redirect('home')

    if isinstance(user, (int, str)):
        user = User.objects.get(id=user)

    with transaction.atomic():
        profile = user.userprofile
        profile.is_fraudulent = True

        confiscated_rewards = profile.rewards
        if confiscated_rewards > 0:
            profile.rewards = 0
            RewardLedger.objects.create(
                user=user,
                amount=-confiscated_rewards,
                transaction_type='fraud_slashing_penalty',
                description=f"Fraud slashing penalty: {reason}"
            )
        profile.save()

        active_tasks = Task.objects.filter(taken_by=user, status='in_progress')
        for task in active_tasks:
            collateral = task.taker_collateral
            if collateral > 0:
                RewardLedger.objects.create(
                    user=user,
                    task=task,
                    amount=-collateral,
                    transaction_type='fraud_slashing_penalty',
                    description=f"Collateral confiscated for task '{task.title}' due to fraud"
                )
                target_poster = harmed_poster or task.posted_by
                if target_poster:
                    poster_profile = target_poster.userprofile
                    poster_profile.rewards += collateral
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=target_poster,
                        task=task,
                        amount=collateral,
                        transaction_type='collateral_slash',
                        description=f"Slashed collateral credited from fraudulent taker: {user.username}"
                    )
                task.taker_collateral = 0
            task.status = 'available'
            task.taken_by = None
            task.save()

    return profile

@login_required(login_url='/login/')
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    context = {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks}
    return render(request, 'my_tasks.html', context)
