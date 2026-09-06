from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

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
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def admin_dispute_panel(request):
    if not request.user.is_staff:
        messages.error(request, "Non-administrative users are blocked from accessing dispute settlement actions.")
        return HttpResponseForbidden("Access denied: Staff status required.")

    open_disputes = Dispute.objects.filter(status='open').order_by('-created_at')
    resolved_disputes = Dispute.objects.filter(status='resolved').order_by('-created_at')

    context = {
        'open_disputes': open_disputes,
        'resolved_disputes': resolved_disputes,
    }
    return render(request, 'admin_dispute_panel.html', context)

@login_required(login_url='/login/')
@require_POST
def settle_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Non-administrative users are blocked from accessing dispute settlement actions.")
        return HttpResponseForbidden("Access denied: Staff status required.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    redirect_target = request.META.get('HTTP_REFERER') or reverse('admin_dispute_panel')

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect(redirect_target)

    task = dispute.task
    if not task.taken_by:
        messages.error(request, "Cannot settle dispute: Task has no assigned taker.")
        return redirect(redirect_target)

    taker_payout_raw = request.POST.get('taker_payout')
    poster_refund_raw = request.POST.get('poster_refund')

    try:
        taker_payout = int(taker_payout_raw)
        poster_refund = int(poster_refund_raw)
    except (ValueError, TypeError):
        messages.error(request, "Payout and refund amounts must be valid integers.")
        return redirect(redirect_target)

    if taker_payout < 0 or poster_refund < 0:
        messages.error(request, "Payout and refund amounts must be non-negative integers.")
        return redirect(redirect_target)

    if taker_payout + poster_refund != task.reward:
        messages.error(
            request,
            f"The sum of taker payout ({taker_payout}) and poster refund ({poster_refund}) "
            f"must equal the total task reward ({task.reward})."
        )
        return redirect(redirect_target)

    with transaction.atomic():
        # Update taker profile balance and record ledger entry
        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += taker_payout
        taker_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=taker_payout,
            transaction_type='dispute_payout',
            description=f"Dispute settlement payout for task: '{task.title}'"
        )

        # Update poster profile balance and record ledger entry
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_refund
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_refund,
            transaction_type='dispute_refund',
            description=f"Dispute settlement refund for task: '{task.title}'"
        )

        # Update dispute and task status
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'completed'
        task.save()

        # Send notifications
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for task '{task.title}' resolved by admin. You received a payout of {taker_payout} points.",
            link=reverse('rewards')
        )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved by admin. You received a refund of {poster_refund} points.",
            link=reverse('rewards')
        )

    messages.success(
        request,
        f"Dispute resolved successfully! Awarded {taker_payout} points to {task.taken_by.username} "
        f"and refunded {poster_refund} points to {task.posted_by.username}."
    )
    return redirect(redirect_target)

