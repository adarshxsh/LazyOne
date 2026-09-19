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
    if request.method != 'POST':
        return redirect('my_tasks')

    reason = request.POST.get('reason')
    if not reason:
        messages.error(request, "A reason is required to raise a dispute.")
        return redirect('my_tasks')

    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, "Task not found.")
            return redirect('my_tasks')

        if hasattr(task, 'dispute') and task.dispute.status == 'open':
            return redirect('dispute_detail', dispute_id=task.dispute.id)

        if task.taken_by != request.user:
            messages.error(request, "You can only raise a dispute for a task you have taken.")
            return redirect('my_tasks')

        if task.status != 'in_progress':
            messages.error(request, f"Cannot raise a dispute for a task with status '{task.status}'.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        user_profile = UserProfile.objects.select_for_update().get(user=request.user)
        if user_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        user_profile.rewards -= deposit_amount
        user_profile.save()

        if hasattr(task, 'dispute'):
            dispute = Dispute.objects.select_for_update().get(id=task.dispute.id)
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

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    with transaction.atomic():
        try:
            dispute = Dispute.objects.select_for_update().get(id=dispute_id)
        except Dispute.DoesNotExist:
            messages.error(request, "Dispute not found.")
            return redirect('my_tasks')

        if dispute.raised_by != request.user:
            messages.error(request, "You are not authorized to withdraw this dispute.")
            return redirect('my_tasks')

        if dispute.status != 'open':
            messages.error(request, "Only open disputes can be withdrawn.")
            return redirect('my_tasks')

        task = Task.objects.select_for_update().get(id=dispute.task_id)
        if task.status != 'disputed':
            messages.error(request, "Task is not currently in disputed status.")
            return redirect('my_tasks')

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
