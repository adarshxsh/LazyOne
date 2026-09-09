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
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to settle this dispute.")
        return redirect('home')

    if dispute.status == 'resolved':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    doer_amount = None
    poster_amount = None

    preset = request.POST.get('split_preset')
    doer_percent_raw = request.POST.get('doer_percent')
    doer_amount_raw = request.POST.get('doer_amount')
    poster_amount_raw = request.POST.get('poster_amount')

    try:
        if doer_amount_raw is not None and doer_amount_raw.strip() != '' and poster_amount_raw is not None and poster_amount_raw.strip() != '':
            doer_amount = int(doer_amount_raw)
            poster_amount = int(poster_amount_raw)
        elif doer_percent_raw is not None and doer_percent_raw.strip() != '':
            doer_percent = float(doer_percent_raw)
            if not (0 <= doer_percent <= 100):
                raise ValueError("Percentage must be between 0 and 100.")
            doer_amount = int(round(task.reward * (doer_percent / 100.0)))
            poster_amount = task.reward - doer_amount
        elif preset:
            if preset == '25_75':
                doer_amount = int(round(task.reward * 0.25))
                poster_amount = task.reward - doer_amount
            elif preset == '50_50':
                doer_amount = int(round(task.reward * 0.50))
                poster_amount = task.reward - doer_amount
            elif preset == '75_25':
                doer_amount = int(round(task.reward * 0.75))
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

        if task.taken_by:
            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += doer_amount
            doer_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=doer_amount,
                transaction_type='dispute_settlement_payout',
                description=f"Dispute settlement payout for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute resolved for task '{task.title}'. You received {doer_amount} points.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += poster_amount
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_amount,
            transaction_type='dispute_settlement_refund',
            description=f"Dispute settlement refund for task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute resolved for task '{task.title}'. You were refunded {poster_amount} points.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        dispute_obj.status = 'resolved'
        dispute_obj.doer_amount = doer_amount
        dispute_obj.poster_amount = poster_amount
        dispute_obj.settled_at = timezone.now()
        dispute_obj.resolved_by = request.user
        dispute_obj.save()

        task.status = 'completed'
        task.save()

    messages.success(request, f"Dispute settled successfully! Doer received {doer_amount} points, Poster refunded {poster_amount} points.")
    return redirect('dispute_detail', dispute_id=dispute.id)

