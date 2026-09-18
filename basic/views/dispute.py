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
@require_POST
def settle_partial_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can settle disputes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if not task.taken_by:
        messages.error(request, "Cannot settle a task that has not been taken by any user.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    resolution_notes = request.POST.get('resolution_notes', '').strip()

    # Determine payout and refund amounts
    split_type = request.POST.get('split_type', '').strip()
    
    try:
        if split_type == 'percentage' or 'taker_percentage' in request.POST:
            taker_pct_str = request.POST.get('taker_percentage', '').strip()
            taker_pct = float(taker_pct_str)
            if taker_pct < 0 or taker_pct > 100:
                raise ValueError("Percentage must be between 0 and 100.")
            taker_payout_amount = int(round(task.reward * (taker_pct / 100.0)))
            poster_refund_amount = task.reward - taker_payout_amount
        else:
            taker_payout_str = request.POST.get('taker_payout_amount', request.POST.get('taker_payout', '')).strip()
            poster_refund_str = request.POST.get('poster_refund_amount', request.POST.get('poster_refund', '')).strip()

            if not taker_payout_str and poster_refund_str:
                poster_refund_amount = int(poster_refund_str)
                taker_payout_amount = task.reward - poster_refund_amount
            elif taker_payout_str and not poster_refund_str:
                taker_payout_amount = int(taker_payout_str)
                poster_refund_amount = task.reward - taker_payout_amount
            elif taker_payout_str and poster_refund_str:
                taker_payout_amount = int(taker_payout_str)
                poster_refund_amount = int(poster_refund_str)
            else:
                messages.error(request, "Please specify a payout or refund amount.")
                return redirect('dispute_detail', dispute_id=dispute_id)

    except (ValueError, TypeError):
        messages.error(request, "Invalid settlement input values.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if taker_payout_amount < 0 or poster_refund_amount < 0:
        messages.error(request, "Payout and refund amounts must be non-negative.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if taker_payout_amount + poster_refund_amount != task.reward:
        messages.error(request, f"The sum of taker payout ({taker_payout_amount}) and poster refund ({poster_refund_amount}) must strictly equal total task reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute_id)

    with transaction.atomic():
        # Update user balances
        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += taker_payout_amount
        taker_profile.save()

        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_refund_amount
        poster_profile.save()

        # Update dispute
        dispute.taker_payout_amount = taker_payout_amount
        dispute.poster_refund_amount = poster_refund_amount
        dispute.resolved_by = request.user
        dispute.resolution_notes = resolution_notes
        dispute.status = 'resolved'
        dispute.save()

        # Update task status
        task.status = 'completed'
        task.save()

        # Record reward ledger entries
        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=taker_payout_amount,
            transaction_type='partial_payout',
            description=f"Partial Task Payout for task: '{task.title}' (Dispute #{dispute.id})"
        )

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_refund_amount,
            transaction_type='partial_refund',
            description=f"Partial Task Refund for task: '{task.title}' (Dispute #{dispute.id})"
        )

        # Create notifications
        notes_text = f" Notes: {resolution_notes}" if resolution_notes else ""
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute resolved for '{task.title}': You received {taker_payout_amount} points payout.{notes_text}",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute resolved for '{task.title}': You received {poster_refund_amount} points refund.{notes_text}",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute successfully resolved. Payout: {taker_payout_amount} points to {task.taken_by.username}, Refund: {poster_refund_amount} points to {task.posted_by.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)
