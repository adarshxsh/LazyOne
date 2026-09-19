import math
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
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not (request.user.is_staff or request.user == task.posted_by or request.user == task.taken_by):
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    percent_str = (
        request.POST.get('percent') or
        request.POST.get('settlement_taker_share_percent') or
        request.POST.get('taker_share_percent')
    )
    notes = (
        request.POST.get('settlement_notes') or
        request.POST.get('notes') or
        request.POST.get('resolution_summary') or
        ''
    )
    deposit_action = request.POST.get('deposit_action') or request.POST.get('deposit_handling') or 'refund'

    try:
        percent = int(percent_str)
        if percent < 0 or percent > 100:
            raise ValueError("Percentage out of range")
    except (ValueError, TypeError):
        messages.error(request, "Invalid percentage. Percentage must be an integer between 0 and 100.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        taker_amount = math.floor(task.reward * (percent / 100.0))
        poster_amount = task.reward - taker_amount

        # Distribute points to taker
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += taker_amount
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_amount,
                transaction_type='dispute_partial_payout',
                description=f"Dispute partial payout ({percent}%) for task: '{task.title}'"
            )

        # Distribute points to poster
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_amount
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_amount,
            transaction_type='dispute_partial_refund',
            description=f"Dispute partial refund ({100 - percent}%) for task: '{task.title}'"
        )

        # Process security deposit bond
        if dispute.escrow_status == 'held':
            if deposit_action == 'forfeit':
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited during dispute resolution for task: '{task.title}'"
                )
            else:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded during dispute resolution for task: '{task.title}'"
                )

        # Transition status and update fields
        dispute.settlement_taker_share_percent = percent
        dispute.settlement_notes = notes
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'completed'
        task.save()

        # Send notifications
        notify_users = [task.posted_by]
        if task.taken_by and task.taken_by != task.posted_by:
            notify_users.append(task.taken_by)

        for user in notify_users:
            Notification.objects.create(
                recipient=user,
                message=f"Dispute for task '{task.title}' resolved with {percent}% taker share.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute resolved successfully with a {percent}% / {100 - percent}% point split.")
    return redirect('dispute_detail', dispute_id=dispute.id)
