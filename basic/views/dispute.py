from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile
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
@require_POST
def settle_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can settle disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    if not task.taken_by:
        messages.error(request, "Cannot settle a dispute for a task without a taker.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    doer_payout_str = request.POST.get('doer_payout')
    poster_refund_str = request.POST.get('poster_refund')

    try:
        doer_payout = int(doer_payout_str)
        poster_refund = int(poster_refund_str)
    except (ValueError, TypeError):
        messages.error(request, "Point allocations must be valid integers.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout < 0 or poster_refund < 0:
        messages.error(request, "Negative point allocations are strictly forbidden.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout + poster_refund != task.reward:
        messages.error(request, f"The total sum of doer payout ({doer_payout}) and poster refund ({poster_refund}) must equal the reserved task reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        doer_profile = task.taken_by.userprofile
        poster_profile = task.posted_by.userprofile

        doer_profile.rewards += doer_payout
        doer_profile.save()

        poster_profile.rewards += poster_refund
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=doer_payout,
            transaction_type='dispute_doer_payout',
            description=f"Dispute settlement payout for task: '{task.title}'"
        )

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_refund,
            transaction_type='dispute_poster_refund',
            description=f"Dispute settlement refund for task: '{task.title}'"
        )

        dispute.status = 'resolved'
        dispute.doer_payout = doer_payout
        dispute.poster_refund = poster_refund
        dispute.save()

        if doer_payout > 0:
            task.status = 'completed'
        else:
            task.status = 'cancelled'
        task.save()

        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for '{task.title}' resolved by staff: {doer_payout} points awarded to you, {poster_refund} points refunded to poster.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for '{task.title}' resolved by staff: {poster_refund} points refunded to you, {doer_payout} points awarded to doer.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute settled successfully: {doer_payout} points to doer, {poster_refund} points to poster.")
    return redirect('dispute_detail', dispute_id=dispute.id)

