from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, UserProfile, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        user_profile = request.user.userprofile
        if user_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def staff_dispute_list(request):
    if not request.user.is_staff:
        messages.error(request, "Access denied. Staff privileges required.")
        return redirect('home')

    disputes = Dispute.objects.select_related('task', 'raised_by', 'task__posted_by', 'task__taken_by').all().order_by('-created_at')

    status_filter = request.GET.get('status', '').strip().lower()
    if status_filter in ['open', 'resolved']:
        disputes = disputes.filter(status=status_filter)

    search_query = request.GET.get('q', '').strip()
    if not search_query:
        search_query = request.GET.get('search', '').strip()

    if search_query:
        disputes = disputes.filter(
            Q(raised_by__username__icontains=search_query) |
            Q(task__posted_by__username__icontains=search_query) |
            Q(task__taken_by__username__icontains=search_query) |
            Q(task__title__icontains=search_query) |
            Q(reason__icontains=search_query)
        )

    context = {
        'disputes': disputes,
        'status_filter': status_filter,
        'search_query': search_query,
    }
    return render(request, 'staff_disputes.html', context)

@login_required(login_url='/login/')
@require_POST
def staff_resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Access denied. Staff privileges required.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status == 'resolved':
        messages.warning(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    action = request.POST.get('action', '').strip().lower()

    poster = task.posted_by
    worker = task.taken_by

    poster_profile, _ = UserProfile.objects.get_or_create(user=poster)
    worker_profile, _ = UserProfile.objects.get_or_create(user=worker) if worker else (None, False)

    with transaction.atomic():
        if action in ['reward_worker', 'worker', 'in_favor_of_worker']:
            if not worker or not worker_profile:
                messages.error(request, "Cannot reward worker because no worker is assigned to this task.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            worker_profile.rewards += task.reward
            worker_profile.save()

            task.status = 'completed'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=worker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Dispute resolved in favor of worker by staff: '{task.title}'"
            )

            Notification.objects.create(
                recipient=worker,
                message=f"Dispute for task '{task.title}' was resolved in your favor. {task.reward} points awarded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            Notification.objects.create(
                recipient=poster,
                message=f"Dispute for task '{task.title}' was resolved in favor of worker ({worker.username}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )

            messages.success(request, f"Dispute resolved in favor of worker ({worker.username}). {task.reward} points awarded.")

        elif action in ['refund_poster', 'poster', 'in_favor_of_poster']:
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Dispute resolved - refund to poster by staff: '{task.title}'"
            )

            Notification.objects.create(
                recipient=poster,
                message=f"Dispute for task '{task.title}' was resolved in your favor. {task.reward} points refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if worker:
                Notification.objects.create(
                    recipient=worker,
                    message=f"Dispute for task '{task.title}' was resolved in favor of poster ({poster.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, f"Dispute resolved in favor of poster ({poster.username}). {task.reward} points refunded.")

        elif action in ['custom', 'custom_adjustment']:
            try:
                poster_points = int(request.POST.get('poster_points', 0))
                worker_points = int(request.POST.get('worker_points', 0))
                if poster_points < 0 or worker_points < 0:
                    raise ValueError("Points cannot be negative.")
            except (ValueError, TypeError):
                messages.error(request, "Invalid point adjustment values provided.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            if poster_points > 0:
                poster_profile.rewards += poster_points
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=poster_points,
                    transaction_type='task_cancellation',
                    description=f"Dispute custom adjustment points refund by staff for task: '{task.title}'"
                )

            if worker_points > 0 and worker and worker_profile:
                worker_profile.rewards += worker_points
                worker_profile.save()
                RewardLedger.objects.create(
                    user=worker,
                    task=task,
                    amount=worker_points,
                    transaction_type='task_completion',
                    description=f"Dispute custom adjustment points awarded by staff for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            if worker_points >= task.reward and worker_points > poster_points:
                task.status = 'completed'
            else:
                task.status = 'cancelled'
            task.save()

            Notification.objects.create(
                recipient=poster,
                message=f"Dispute for task '{task.title}' was resolved by staff with custom adjustment: {poster_points} points returned.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if worker:
                Notification.objects.create(
                    recipient=worker,
                    message=f"Dispute for task '{task.title}' was resolved by staff with custom adjustment: {worker_points} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, f"Dispute resolved with custom adjustments ({poster_points} pts to poster, {worker_points} pts to worker).")

        else:
            messages.error(request, "Invalid resolution action selected.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('staff_disputes')

