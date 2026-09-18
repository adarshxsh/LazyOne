from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
from ..services.reputation import ReputationService
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

            ReputationService.record_dispute_raised(request.user.userprofile)

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
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    # Only staff/moderator or task participants can resolve
    if not request.user.is_staff and request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    winner_side = request.POST.get('winner') # 'doer' or 'poster'
    if winner_side not in ['doer', 'poster']:
        messages.error(request, "Invalid dispute winner selection.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    with transaction.atomic():
        doer_profile = task.taken_by.userprofile
        poster_profile = task.posted_by.userprofile

        if winner_side == 'doer':
            total_payout = task.reward + task.taker_collateral
            doer_profile.rewards += total_payout
            doer_profile.save()
            
            extra_poster_collateral = task.poster_collateral - task.reward
            if extra_poster_collateral > 0:
                poster_profile.rewards += extra_poster_collateral
                poster_profile.save()

            task.status = 'completed'
            task.save()
            dispute.status = 'resolved'
            dispute.save()
            dispute.refund_deposit(reason_description=f"Security deposit bond refunded for won dispute on task: '{task.title}'")
            doer_profile.refresh_from_db()

            ReputationService.record_dispute_won(doer_profile)
            ReputationService.record_dispute_lost(poster_profile)

            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.reward,
                transaction_type='task_completion', description=f"Dispute resolved in favor of doer: '{task.title}'"
            )
            if task.taker_collateral > 0:
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=task.taker_collateral,
                    transaction_type='collateral_refund', description=f"Security deposit returned: '{task.title}'"
                )
            messages.success(request, f"Dispute resolved in favor of doer ({task.taken_by.username}).")

        else: # poster wins
            poster_refund = task.poster_collateral + task.taker_collateral
            poster_profile.rewards += poster_refund
            poster_profile.save()

            task.status = 'cancelled'
            task.save()
            dispute.status = 'resolved'
            dispute.save()
            dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Security deposit bond forfeited for lost dispute on task: '{task.title}'")
            poster_profile.refresh_from_db()

            ReputationService.record_dispute_won(poster_profile)
            ReputationService.record_dispute_lost(doer_profile)

            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=poster_refund,
                transaction_type='task_cancellation', description=f"Dispute resolved in favor of poster: '{task.title}'"
            )
            if task.taker_collateral > 0:
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=-task.taker_collateral,
                    transaction_type='collateral_forfeit', description=f"Security deposit forfeited in dispute: '{task.title}'"
                )
            messages.success(request, f"Dispute resolved in favor of poster ({task.posted_by.username}).")

    return redirect('my_tasks')
