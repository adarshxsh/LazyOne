from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

DISPUTE_DEPOSIT_BOND = 50

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

        user_profile = request.user.userprofile
        if user_profile.rewards < DISPUTE_DEPOSIT_BOND:
            messages.error(
                request,
                f"You need at least {DISPUTE_DEPOSIT_BOND} points as a deposit bond to raise a dispute. Your current balance is {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        with transaction.atomic():
            user_profile.rewards -= DISPUTE_DEPOSIT_BOND
            user_profile.save()

            dispute = Dispute.objects.create(
                task=task,
                raised_by=request.user,
                reason=reason,
                deposit_amount=DISPUTE_DEPOSIT_BOND
            )
            task.status = 'disputed'
            task.save()

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-DISPUTE_DEPOSIT_BOND,
                transaction_type='dispute_deposit',
                description=f"Deposit bond held for dispute on task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, f"Dispute raised successfully. {DISPUTE_DEPOSIT_BOND} points deposit bond held.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    deposit_amount = dispute.deposit_amount

    with transaction.atomic():
        user_profile = request.user.userprofile
        user_profile.rewards += deposit_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=deposit_amount,
            transaction_type='dispute_refund',
            description=f"Deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )

        task.status = 'in_progress'
        task.save()
        dispute.delete()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Refunded {deposit_amount} deposit bond points.")
    return redirect('my_tasks')
