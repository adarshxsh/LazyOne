from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.db import transaction
from django.utils import timezone
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
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Only staff members can resolve disputes.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open' or task.status != 'disputed':
        messages.error(request, "This dispute cannot be resolved because it is not currently open and disputed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    doer_payout_str = request.POST.get('doer_payout')
    poster_refund_str = request.POST.get('poster_refund')
    resolution_notes = request.POST.get('resolution_notes', '')

    try:
        doer_payout = int(doer_payout_str)
        poster_refund = int(poster_refund_str)
    except (ValueError, TypeError):
        messages.error(request, "Doer payout and poster refund must be valid non-negative integers.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout < 0 or poster_refund < 0:
        messages.error(request, "Payout amounts must be non-negative integers.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout + poster_refund != task.reward:
        messages.error(request, f"The sum of doer payout ({doer_payout}) and poster refund ({poster_refund}) must strictly equal the total task reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if doer_payout > 0 and task.taken_by:
            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += doer_payout
            doer_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=doer_payout,
                transaction_type='dispute_payout',
                description=f"Dispute settlement payout for task: '{task.title}'"
            )

        if poster_refund > 0:
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

        dispute.doer_payout = doer_payout
        dispute.poster_refund = poster_refund
        dispute.resolution_notes = resolution_notes
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'completed'
        task.save()

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved: You received {doer_payout} points payout.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved: You received {poster_refund} points refund.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Dispute has been settled and point balances updated.")
    return redirect('dispute_detail', dispute_id=dispute.id)

