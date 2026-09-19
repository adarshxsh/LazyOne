import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
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

    if not (request.user.is_staff or request.user == task.posted_by or request.user == task.taken_by):
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    if dispute.status == 'resolved':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    preset = request.POST.get('split_preset')
    percent_str = (
        request.POST.get('percent') or
        request.POST.get('doer_percent') or
        request.POST.get('taker_percent') or
        request.POST.get('settlement_taker_share_percent') or
        request.POST.get('taker_share_percent')
    )
    doer_amount_raw = request.POST.get('doer_amount') or request.POST.get('taker_amount')
    poster_amount_raw = request.POST.get('poster_amount')
    notes = (
        request.POST.get('settlement_notes') or
        request.POST.get('notes') or
        request.POST.get('resolution_summary') or
        ''
    )
    deposit_action = request.POST.get('deposit_action') or request.POST.get('deposit_handling') or 'refund'

    doer_amount = None
    poster_amount = None

    try:
        if doer_amount_raw is not None and doer_amount_raw.strip() != '' and poster_amount_raw is not None and poster_amount_raw.strip() != '':
            doer_amount = int(doer_amount_raw)
            poster_amount = int(poster_amount_raw)
        elif percent_str is not None and percent_str.strip() != '':
            percent_val = float(percent_str)
            if not (0 <= percent_val <= 100):
                raise ValueError("Percentage must be between 0 and 100.")
            doer_amount = math.floor(task.reward * (percent_val / 100.0))
            poster_amount = task.reward - doer_amount
        elif preset:
            if preset == '25_75':
                doer_amount = math.floor(task.reward * 0.25)
                poster_amount = task.reward - doer_amount
            elif preset == '50_50':
                doer_amount = math.floor(task.reward * 0.50)
                poster_amount = task.reward - doer_amount
            elif preset == '75_25':
                doer_amount = math.floor(task.reward * 0.75)
                poster_amount = task.reward - doer_amount
            elif preset == '100_0':
                doer_amount = task.reward
                poster_amount = 0
            elif preset == '0_100':
                doer_amount = 0
                poster_amount = task.reward
            else:
                raise ValueError("Invalid split preset.")
        elif doer_amount_raw is not None and doer_amount_raw.strip() != '':
            doer_amount = int(doer_amount_raw)
            poster_amount = task.reward - doer_amount
        elif poster_amount_raw is not None and poster_amount_raw.strip() != '':
            poster_amount = int(poster_amount_raw)
            doer_amount = task.reward - poster_amount
        else:
            messages.error(request, "Please provide a valid settlement amount or percentage.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    except (ValueError, TypeError):
        messages.error(request, "Invalid settlement input.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_amount < 0 or poster_amount < 0:
        messages.error(request, "Settlement amounts must be non-negative integers.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_amount + poster_amount != task.reward:
        messages.error(request, f"The sum of doer amount ({doer_amount}) and poster amount ({poster_amount}) must equal task reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute_obj = Dispute.objects.select_for_update().get(id=dispute.id)
        if dispute_obj.status == 'resolved':
            messages.error(request, "This dispute has already been resolved.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        taker_share_percent = int(round((doer_amount / task.reward) * 100)) if task.reward > 0 else 0

        # Payout doer / taker
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += doer_amount
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=doer_amount,
                transaction_type='dispute_partial_payout',
                description=f"Dispute partial payout ({taker_share_percent}%) for task: '{task.title}'"
            )

        # Refund poster
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_amount
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_amount,
            transaction_type='dispute_partial_refund',
            description=f"Dispute partial refund ({100 - taker_share_percent}%) for task: '{task.title}'"
        )

        # Process deposit bond
        if dispute_obj.escrow_status == 'held':
            if deposit_action == 'forfeit':
                dispute_obj.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited during dispute resolution for task: '{task.title}'"
                )
            else:
                dispute_obj.refund_deposit(
                    reason_description=f"Security deposit bond refunded during dispute resolution for task: '{task.title}'"
                )

        # Update dispute record
        dispute_obj.doer_amount = doer_amount
        dispute_obj.poster_amount = poster_amount
        dispute_obj.settlement_taker_share_percent = taker_share_percent
        dispute_obj.settlement_notes = notes
        dispute_obj.status = 'resolved'
        dispute_obj.settled_at = timezone.now()
        dispute_obj.resolved_by = request.user
        dispute_obj.save()

        # Update task status
        task.status = 'completed'
        task.save()

        # Issue notifications
        notify_users = [task.posted_by]
        if task.taken_by and task.taken_by != task.posted_by:
            notify_users.append(task.taken_by)

        for user in notify_users:
            Notification.objects.create(
                recipient=user,
                message=f"Dispute for task '{task.title}' resolved with {taker_share_percent}% taker share ({doer_amount} points).",
                link=reverse('dispute_detail', args=[dispute_obj.id])
            )

    messages.success(request, f"Dispute settled successfully! Doer received {doer_amount} points, Poster refunded {poster_amount} points.")
    return redirect('dispute_detail', dispute_id=dispute.id)

settle_dispute = resolve_dispute
