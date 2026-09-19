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

        deposit_amount = task.deposit_bond_amount_for_user(request.user)
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

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    winner_type = request.POST.get('winner')  # 'poster' or 'taker'
    if winner_type not in ['poster', 'taker']:
        messages.error(request, "Invalid dispute resolution choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_user = task.posted_by if winner_type == 'poster' else task.taken_by
    loser_user = task.taken_by if winner_type == 'poster' else task.posted_by

    with transaction.atomic():
        dispute.status = 'resolved'
        if dispute.escrow_status == 'held':
            if winner_user == dispute.raised_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                )
                winner_profile = winner_user.userprofile
                winner_profile.disputes_won += 1
                winner_profile.update_reputation(15, save=False)
                winner_profile.save()

                loser_profile = loser_user.userprofile
                loser_profile.disputes_lost += 1
                loser_profile.update_reputation(-25, save=False)
                loser_profile.save()
            else:
                dispute.forfeit_deposit(
                    beneficiary=winner_user,
                    reason_description=f"Security deposit bond forfeited to {winner_user.username} upon winning dispute for task: '{task.title}'"
                )
        else:
            dispute.save()
            winner_profile = winner_user.userprofile
            winner_profile.disputes_won += 1
            winner_profile.update_reputation(15, save=False)
            winner_profile.save()

            loser_profile = loser_user.userprofile
            loser_profile.disputes_lost += 1
            loser_profile.update_reputation(-25, save=False)
            loser_profile.save()

        if winner_type == 'taker':
            task.status = 'completed'
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.tasks_completed += 1
            taker_profile.update_reputation(10, save=False)
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Completed task via dispute resolution: '{task.title}'"
            )
        else:
            task.status = 'cancelled'
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for disputed task: '{task.title}'"
            )
        task.save()

        Notification.objects.create(
            recipient=loser_user,
            message=f"Dispute for task '{task.title}' has been resolved in favor of {winner_user.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=winner_user,
            message=f"Dispute for task '{task.title}' has been resolved in your favor.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute resolved in favor of {winner_user.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)
