from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    if request.method == 'POST':
        return resolve_dispute(request, dispute_id)
    can_resolve = (dispute.status == 'open') and (request.user == task.posted_by or request.user.is_staff)
    context = {
        'dispute': dispute,
        'task': task,
        'can_resolve': can_resolve,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def resolve_dispute(request, dispute_id):
    if request.method != 'POST':
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    doer_payout_raw = request.POST.get('doer_payout') or request.POST.get('doer_amount') or request.POST.get('taker_payout')
    poster_refund_raw = request.POST.get('poster_refund') or request.POST.get('poster_amount')

    try:
        if doer_payout_raw is not None and poster_refund_raw is not None and str(doer_payout_raw).strip() != '' and str(poster_refund_raw).strip() != '':
            doer_payout = int(doer_payout_raw)
            poster_refund = int(poster_refund_raw)
        elif doer_payout_raw is not None and str(doer_payout_raw).strip() != '':
            doer_payout = int(doer_payout_raw)
            poster_refund = task.reward - doer_payout
        elif poster_refund_raw is not None and str(poster_refund_raw).strip() != '':
            poster_refund = int(poster_refund_raw)
            doer_payout = task.reward - poster_refund
        else:
            messages.error(request, "Please specify valid payout amounts.")
            return redirect('dispute_detail', dispute_id=dispute.id)
    except (ValueError, TypeError):
        messages.error(request, "Payout and refund amounts must be valid integers.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout < 0 or poster_refund < 0:
        messages.error(request, "Payout and refund amounts cannot be negative.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if doer_payout + poster_refund != task.reward:
        messages.error(request, f"The sum of payouts ({doer_payout + poster_refund}) must equal total task reward ({task.reward}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if task.taken_by:
            doer_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
            doer_profile.rewards += doer_payout
            doer_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=doer_payout,
                transaction_type='dispute_payout',
                description=f"Dispute payout for task: '{task.title}'"
            )

        poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
        poster_profile.rewards += poster_refund
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=poster_refund,
            transaction_type='dispute_refund',
            description=f"Dispute refund for task: '{task.title}'"
        )

        dispute.status = 'resolved'
        dispute.doer_payout = doer_payout
        dispute.poster_refund = poster_refund
        dispute.save()

        task.status = 'completed'
        task.save()

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute resolved for '{task.title}'. You received {doer_payout} points payout.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute resolved for '{task.title}'. You received {poster_refund} points refund.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute resolved successfully with split: {doer_payout} points to doer, {poster_refund} points to poster.")
    return redirect('dispute_detail', dispute_id=dispute.id)

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
