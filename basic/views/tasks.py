from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, RewardLedger
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.db.models import Q
from datetime import datetime, timedelta
from ..stripe_utils import (
    create_escrow_payment_intent,
    payout_to_connected_account,
    refund_escrow_payment
)


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

            reward_type = request.POST.get('reward_type', 'points')
            if reward_type not in ['points', 'usd']:
                reward_type = 'points'

            user_profile = request.user.userprofile
            if reward_type == 'points' and user_profile.rewards < reward:
                messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            deadline = timezone.make_aware(datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M'))
            if deadline <= timezone.now():
                messages.error(request, "Deadline must be in the future.")
                default_deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                return render(request, 'add_task.html', {'default_deadline': default_deadline})

            with transaction.atomic():
                if reward_type == 'points':
                    user_profile.rewards -= reward
                    user_profile.save()
                    new_task = Task.objects.create(
                        title=title, description=description, reward=reward,
                        posted_by=request.user, deadline=deadline, status='available',
                        reward_type='points'
                    )
                    RewardLedger.objects.create(
                        user=request.user, task=new_task, amount=-reward, currency='points',
                        transaction_type='task_creation', description=f"Reserved for task: '{title}'"
                    )
                    messages.success(request, f'Task added successfully! {reward} points have been reserved.')
                else:
                    try:
                        stripe_payment_intent_id = create_escrow_payment_intent(
                            amount_cents=reward * 100,
                            description=f"Escrow hold for USD task: '{title}'"
                        )
                    except Exception as e:
                        raise ValueError(f"Stripe payment pre-authorization failed: {str(e)}")

                    new_task = Task.objects.create(
                        title=title, description=description, reward=reward,
                        posted_by=request.user, deadline=deadline, status='available',
                        reward_type='usd', stripe_payment_intent_id=stripe_payment_intent_id
                    )
                    RewardLedger.objects.create(
                        user=request.user, task=new_task, amount=-reward * 100, currency='usd',
                        transaction_type='task_creation', description=f"USD Escrow Hold for task: '{title}'"
                    )
                    messages.success(request, f'Task added successfully! ${reward}.00 has been secured and locked in escrow.')
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
        if task.reward_type == 'usd':
            taker_profile = request.user.userprofile
            if not taker_profile.stripe_account_id:
                messages.error(request, "You must connect your Stripe account before taking USD tasks. Connect it on your profile page.")
                return redirect('profile')

        with transaction.atomic():
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
    task = get_object_or_404(Task, Q(status='in_progress') | Q(status='disputed'), id=task_id, posted_by=request.user)
    
    if task.reward_type == 'usd':
        worker_profile = task.taken_by.userprofile
        if not worker_profile.stripe_account_id:
            messages.error(request, "The worker has not linked a Stripe Connect account. They must link a Stripe account before you can complete the task.")
            return redirect('my_tasks')
            
        with transaction.atomic():
            try:
                stripe_transfer_id = payout_to_connected_account(
                    stripe_payment_intent_id=task.stripe_payment_intent_id,
                    amount_cents=task.reward * 100,
                    worker_stripe_account_id=worker_profile.stripe_account_id,
                    description=f"Task payout for completion: '{task.title}'"
                )
                task.stripe_transfer_id = stripe_transfer_id
                task.status = 'completed'
                task.save()
                
                if hasattr(task, 'dispute'):
                    task.dispute.status = 'resolved'
                    task.dispute.save()
                
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=task.reward * 100, currency='usd',
                    transaction_type='task_completion', description=f"Completed task payout: '{task.title}'"
                )
                messages.success(request, f"Task marked as complete! ${task.reward}.00 transferred to {task.taken_by.username}'s Stripe account.")
            except Exception as e:
                messages.error(request, f"Stripe payment transfer failed: {str(e)}")
                return redirect('my_tasks')
        return redirect('my_tasks')

    with transaction.atomic():
        task_doer_profile = task.taken_by.userprofile
        task_doer_profile.rewards += task.reward
        task_doer_profile.save()
        task.status = 'completed'
        task.save()

        if hasattr(task, 'dispute'):
            task.dispute.status = 'resolved'
            task.dispute.save()

        RewardLedger.objects.create(
            user=task.taken_by, task=task, amount=task.reward, currency='points',
            transaction_type='task_completion', description=f"Completed task: '{task.title}'"
        )
        messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def cancel_task(request, task_id):
    task = get_object_or_404(Task, id=task_id, posted_by=request.user, status='available')
    
    if task.reward_type == 'usd':
        with transaction.atomic():
            try:
                stripe_refund_id = refund_escrow_payment(task.stripe_payment_intent_id)
                task.stripe_refund_id = stripe_refund_id
                task.status = 'cancelled'
                task.save()
                
                RewardLedger.objects.create(
                    user=request.user, task=task, amount=task.reward * 100, currency='usd',
                    transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
                )
                messages.success(request, "You have cancelled the task and your USD reward has been refunded via Stripe.")
            except Exception as e:
                messages.error(request, f"Stripe refund failed: {str(e)}")
                return redirect('my_tasks')
        return redirect('my_tasks')

    with transaction.atomic():
        task.status = 'cancelled'
        task.save()
        user_profile = request.user.userprofile
        user_profile.rewards += task.reward
        user_profile.save()
        RewardLedger.objects.create(
            user=request.user, task=task, amount=task.reward, currency='points',
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
    
    if task.reward_type == 'usd':
        with transaction.atomic():
            try:
                stripe_refund_id = refund_escrow_payment(task.stripe_payment_intent_id)
                task.stripe_refund_id = stripe_refund_id
                task.status = 'cancelled'
                task.taken_by = None
                task.cancellation_requested = False
                task.save()
                
                RewardLedger.objects.create(
                    user=task.posted_by, task=task, amount=task.reward * 100, currency='usd',
                    transaction_type='task_cancellation', description=f"Refund for cancelled task: '{task.title}'"
                )
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"{request.user.username} accepted your cancellation request for '{task.title}'. The task has been cancelled and refunded.",
                    link=reverse('my_tasks')
                )
                messages.success(request, "You have accepted the cancellation. The task is cancelled and refunded.")
            except Exception as e:
                messages.error(request, f"Stripe refund failed: {str(e)}")
                return redirect('my_tasks')
        return redirect('my_tasks')

    with transaction.atomic():
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by, task=task, amount=task.reward, currency='points',
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
    task = get_object_or_404(Task, id=task_id, taken_by=request.user, status='in_progress')
    with transaction.atomic():
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

@login_required(login_url='/login/')
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    context = {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks}
    return render(request, 'my_tasks.html', context)
