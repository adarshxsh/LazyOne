import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, DisputeJuror, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..juror_selection import select_and_assign_jurors, unlock_dispute_juror_stakes

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    assigned_jurors = dispute.jurors.select_related('user').all()
    is_juror = assigned_jurors.filter(user=request.user).exists()

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_juror_record = assigned_jurors.filter(user=request.user).first() if is_juror else None

    context = {
        'dispute': dispute,
        'task': task,
        'jurors': assigned_jurors,
        'is_juror': is_juror,
        'user_juror_record': user_juror_record
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

            # Trigger automated dispute juror selection
            select_and_assign_jurors(dispute)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        unlock_dispute_juror_stakes(dispute, reason_description=f"Juror stake refunded for withdrawn dispute on task: '{task.title}'")

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
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    juror_record = get_object_or_404(DisputeJuror, dispute=dispute, user=request.user)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['posted_by', 'taken_by']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_record.vote = vote_choice
        juror_record.voted_at = timezone.now()
        juror_record.save()

        messages.success(request, "Your vote has been cast.")

        # Check if all assigned jurors have voted
        total_jurors = dispute.jurors.count()
        voted_jurors = dispute.jurors.filter(vote__isnull=False)

        if total_jurors > 0 and voted_jurors.count() == total_jurors:
            posted_by_votes = dispute.jurors.filter(vote='posted_by').count()
            taken_by_votes = dispute.jurors.filter(vote='taken_by').count()

            winning_choice = 'posted_by' if posted_by_votes > taken_by_votes else 'taken_by'
            winning_jurors = list(dispute.jurors.filter(vote=winning_choice))
            minority_jurors = list(dispute.jurors.exclude(vote=winning_choice))

            total_forfeited_stakes = sum(j.stake_amount for j in minority_jurors)
            reward_share_per_winning_juror = math.floor(total_forfeited_stakes / len(winning_jurors)) if winning_jurors else 0

            # Refund stake and award reward share to winning jurors
            for juror in winning_jurors:
                j_profile = juror.user.userprofile
                j_profile.rewards += juror.stake_amount + reward_share_per_winning_juror
                j_profile.save()

                RewardLedger.objects.create(
                    user=juror.user,
                    task=dispute.task,
                    amount=juror.stake_amount,
                    transaction_type='juror_stake_refunded',
                    description=f"Stake refunded for winning vote on dispute: '{dispute.task.title}'"
                )

                if reward_share_per_winning_juror > 0:
                    RewardLedger.objects.create(
                        user=juror.user,
                        task=dispute.task,
                        amount=reward_share_per_winning_juror,
                        transaction_type='juror_reward',
                        description=f"Juror reward share awarded for dispute on task: '{dispute.task.title}'"
                    )

                juror.is_stake_locked = False
                juror.save()

            # Minority jurors lose locked stake (handled per governance rules)
            for juror in minority_jurors:
                juror.is_stake_locked = False
                juror.save()

            # Process dispute resolution for task poster / taker
            task = dispute.task
            if winning_choice == 'posted_by':
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for task resolved in poster's favor: '{task.title}'"
                )

                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit()
                else:
                    dispute.forfeit_deposit(beneficiary=task.posted_by)

                task.status = 'cancelled'
                task.save()
            else:
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded reward for dispute resolved in worker's favor: '{task.title}'"
                    )

                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit()
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by)

                task.status = 'completed'
                task.save()

            dispute.status = 'resolved'
            dispute.save()

    return redirect('dispute_detail', dispute_id=dispute.id)
