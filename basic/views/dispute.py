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

    deposit_amount = task.get_deposit_bond()
    user_profile = request.user.userprofile

    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points. You need a deposit bond of {deposit_amount} points to raise a dispute, but you only have {user_profile.rewards} points."
        )
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            dispute = Dispute.objects.create(
                task=task,
                raised_by=request.user,
                reason=reason,
                deposit_amount=deposit_amount,
                deposit_status='held',
                status='open'
            )

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit_hold',
                description=f"Dispute deposit hold for task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held in escrow as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task

    with transaction.atomic():
        refund_amount = dispute.deposit_amount
        dispute.resolve_deposit('refund')
        task.status = 'in_progress'
        task.save()
        dispute.delete()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Deposit bond of {refund_amount} points refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    outcome = request.POST.get('outcome', 'refund')
    if outcome not in ['refund', 'forfeit']:
        messages.error(request, "Invalid resolution outcome.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolve_deposit(outcome)
        dispute.save()

        if outcome == 'refund':
            messages.success(request, f"Dispute resolved. Deposit bond of {dispute.deposit_amount} points refunded to {dispute.raised_by.username}.")
        else:
            messages.success(request, f"Dispute resolved. Deposit bond of {dispute.deposit_amount} points forfeited by {dispute.raised_by.username}.")

    return redirect('dispute_detail', dispute_id=dispute.id)

