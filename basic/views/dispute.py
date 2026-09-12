from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
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
def propose_settlement(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to propose a settlement for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    offered_doer_amount_raw = request.POST.get('offered_doer_amount')
    try:
        offered_doer_amount = int(offered_doer_amount_raw)
        if offered_doer_amount < 0 or offered_doer_amount > task.reward:
            raise ValueError("Amount out of bounds")
    except (ValueError, TypeError):
        messages.error(request, f"Offered doer amount must be a whole integer between 0 and {task.reward}.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.offered_doer_amount = offered_doer_amount
    dispute.offered_by = request.user
    dispute.save()

    counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"{request.user.username} has proposed a partial settlement of {offered_doer_amount} points for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Settlement proposal submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def accept_settlement(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to accept settlement for this dispute.")
        return redirect('home')

    if dispute.status != 'open' or dispute.offered_doer_amount is None or dispute.offered_by is None:
        messages.error(request, "There is no active settlement proposal to accept.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == dispute.offered_by:
        messages.error(request, "You cannot accept your own settlement proposal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    doer_amount = dispute.offered_doer_amount
    poster_amount = task.reward - doer_amount

    with transaction.atomic():
        if task.taken_by and hasattr(task.taken_by, 'userprofile'):
            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += doer_amount
            doer_profile.save()

        if task.posted_by and hasattr(task.posted_by, 'userprofile'):
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += poster_amount
            poster_profile.save()

        if task.taken_by:
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=doer_amount,
                transaction_type='dispute_payout',
                description=f"Dispute settlement payout for task: '{task.title}'"
            )

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_amount,
            transaction_type='dispute_refund',
            description=f"Dispute settlement refund for task: '{task.title}'"
        )

        dispute.status = 'resolved'
        dispute.resolution_type = 'partial_split'
        dispute.resolved_doer_amount = doer_amount
        dispute.resolved_poster_amount = poster_amount
        dispute.save()

        task.status = 'completed'
        task.save()

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved. You received {doer_amount} points.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved. You received a refund of {poster_amount} points.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Settlement accepted and rewards distributed.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def reject_settlement(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to reject settlement for this dispute.")
        return redirect('home')

    if dispute.status != 'open' or dispute.offered_doer_amount is None or dispute.offered_by is None:
        messages.error(request, "There is no active settlement proposal to reject.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == dispute.offered_by:
        messages.error(request, "You cannot reject your own settlement proposal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    proposer = dispute.offered_by
    dispute.offered_doer_amount = None
    dispute.offered_by = None
    dispute.save()

    Notification.objects.create(
        recipient=proposer,
        message=f"{request.user.username} has rejected your settlement proposal for task '{task.title}'.",
        link=reverse('dispute_detail', args=[dispute.id])
    )

    messages.success(request, "Settlement proposal rejected.")
    return redirect('dispute_detail', dispute_id=dispute.id)
