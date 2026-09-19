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
    if request.user != task.taken_by and request.user != task.posted_by:
        messages.error(request, "You are not authorized to raise a dispute on this task.")
        return redirect('my_tasks')
    if task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task currently in progress.")
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

            recipient = task.posted_by if request.user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
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

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "You are not authorized to perform administrative dispute resolutions.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task
    decision = request.POST.get('decision')

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.save()

        if decision == 'favour_taker':
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                collateral_amount = task.deposit_bond_amount
                taker_profile.rewards += (task.reward + collateral_amount)
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward awarded for resolved dispute on task: '{task.title}'"
                )
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=collateral_amount,
                    transaction_type='collateral_refund',
                    description=f"Collateral refunded for resolved dispute on task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Security deposit bond refunded for dispute resolved in your favor on task: '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Dispute deposit bond forfeited for dispute resolved in taker's favor on task: '{task.title}'")

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in your favor by administration.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of taker by administration.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            messages.success(request, f"Dispute resolved in favor of taker ({task.taken_by.username if task.taken_by else ''}).")

        else: # decision == 'favour_poster'
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for resolved dispute on task: '{task.title}'"
            )

            if task.taken_by:
                collateral_amount = task.deposit_bond_amount
                poster_profile.rewards += collateral_amount
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=0,
                    transaction_type='collateral_slashed',
                    description=f"Collateral slashed for fraudulent default on task: '{task.title}'"
                )
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=collateral_amount,
                    transaction_type='dispute_refund',
                    description=f"Slashed taker collateral awarded as compensation for task: '{task.title}'"
                )

            task.status = 'cancelled'
            task.save()

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(reason_description=f"Security deposit bond refunded for dispute resolved in your favor on task: '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Dispute deposit bond forfeited for dispute resolved in poster's favor on task: '{task.title}'")

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved against you. Collateral slashed.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor by administration.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            messages.success(request, f"Dispute resolved in favor of poster ({task.posted_by.username}). Taker collateral slashed.")

    return redirect('dispute_detail', dispute_id=dispute.id)
