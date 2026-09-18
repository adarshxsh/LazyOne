from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
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
    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'dispute_detail.html', context)

def resolve_dispute(dispute, winner=None, voting_jurors=None):
    """
    Resolves a dispute by allocating a fixed 10% fee to voting jurors and
    transferring the remaining reward points to the winning party.
    All point transfers and status updates are executed within a single transaction.atomic() block.
    """
    if dispute.status == 'resolved':
        return

    task = dispute.task
    total_reward = task.reward

    with transaction.atomic():
        juror_list = list(voting_jurors) if voting_jurors else []
        num_jurors = len(juror_list)

        if num_jurors > 0:
            juror_pool = int(total_reward * 0.10)
            fee_per_juror = juror_pool // num_jurors
            actual_juror_total = fee_per_juror * num_jurors
        else:
            juror_pool = 0
            fee_per_juror = 0
            actual_juror_total = 0

        settlement_payout = total_reward - actual_juror_total

        if fee_per_juror > 0:
            for juror in juror_list:
                juror_profile, _ = UserProfile.objects.get_or_create(user=juror)
                juror_profile.rewards += fee_per_juror
                juror_profile.save()

                RewardLedger.objects.create(
                    user=juror,
                    task=task,
                    amount=fee_per_juror,
                    transaction_type='juror_reward',
                    description=f"Juror reward for dispute resolution on task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.save()

        if winner == task.taken_by or winner == 'taker':
            if hasattr(dispute, 'refund_deposit') and dispute.escrow_status == 'held':
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon dispute resolution for task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()

            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += settlement_payout
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=settlement_payout,
                    transaction_type='dispute_settlement',
                    description=f"Dispute settlement payout for task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for '{task.title}' resolved in your favor! {settlement_payout} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' resolved in favor of worker ({task.taken_by.username if task.taken_by else 'taker'}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        else:
            if hasattr(dispute, 'forfeit_deposit') and dispute.escrow_status == 'held':
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited upon dispute resolution for task: '{task.title}'"
                )
            task.status = 'cancelled'
            task.save()

            poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
            poster_profile.rewards += settlement_payout
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=settlement_payout,
                transaction_type='dispute_refund',
                description=f"Dispute settlement refund for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' resolved in your favor! {settlement_payout} points refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for '{task.title}' resolved in favor of task poster ({task.posted_by.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

@login_required(login_url='/login/')
@require_POST
def resolve_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if not request.user.is_staff and request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    winner = request.POST.get('winner')  # 'poster' or 'taker'
    resolve_dispute(dispute, winner=winner)
    messages.success(request, f"Dispute for '{task.title}' has been resolved.")
    return redirect('dispute_detail', dispute_id=dispute.id)

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
