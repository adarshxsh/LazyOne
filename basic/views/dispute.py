from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone

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
def resolve_dispute(request, dispute_id):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Only staff members or superusers can resolve disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    if not task.taken_by:
        messages.error(request, "Cannot resolve dispute for a task without an assigned doer.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    poster_amount_str = request.POST.get('poster_amount')
    doer_amount_str = request.POST.get('doer_amount')
    resolution_notes = request.POST.get('resolution_notes', '').strip()

    if (poster_amount_str is None or doer_amount_str is None or poster_amount_str == '' or doer_amount_str == '') and request.POST.get('poster_percent') is not None:
        try:
            poster_percent = float(request.POST.get('poster_percent'))
            if 0 <= poster_percent <= 100:
                poster_amount = round(task.reward * (poster_percent / 100.0))
                doer_amount = task.reward - poster_amount
            else:
                messages.error(request, "Percentage must be between 0 and 100.")
                return redirect('dispute_detail', dispute_id=dispute.id)
        except (ValueError, TypeError):
            messages.error(request, "Invalid percentage value.")
            return redirect('dispute_detail', dispute_id=dispute.id)
    else:
        try:
            poster_amount = int(poster_amount_str)
            doer_amount = int(doer_amount_str)
        except (ValueError, TypeError):
            messages.error(request, "Poster payout and doer payout must be valid non-negative integers.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    if poster_amount < 0 or doer_amount < 0:
        messages.error(request, "Payout amounts cannot be negative.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if poster_amount + doer_amount != task.reward:
        messages.error(request, f"Total split ({poster_amount} + {doer_amount}) must equal total escrowed reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon staff dispute resolution for task: '{task.title}'"
            )

        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_amount
        poster_profile.save()

        doer_profile = task.taken_by.userprofile
        doer_profile.rewards += doer_amount
        doer_profile.save()

        task.status = 'completed'
        task.save()

        dispute.status = 'resolved'
        dispute.poster_amount = poster_amount
        dispute.doer_amount = doer_amount
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.resolution_notes = resolution_notes
        dispute.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_amount,
            transaction_type='dispute_settlement',
            description=f"Dispute settlement refund for task: '{task.title}'"
        )
        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=doer_amount,
            transaction_type='dispute_settlement',
            description=f"Dispute settlement payout for task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved. Refunded: {poster_amount} points.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for task '{task.title}' resolved. Awarded: {doer_amount} points.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute resolved successfully. Poster received {poster_amount} pts, Taker received {doer_amount} pts.")
    return redirect('dispute_detail', dispute_id=dispute.id)

