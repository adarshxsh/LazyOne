from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, Jury, Juror, Vote
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..jury import create_jury_for_dispute, cast_juror_vote, refund_juror_stakes_on_withdrawal


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    jury = getattr(dispute, 'jury', None)
    juror = Juror.objects.filter(jury=jury, user=request.user).first() if jury else None

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = (juror is not None)

    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    can_vote = (juror is not None and juror.status == 'assigned' and dispute.status == 'open' and jury and jury.status == 'voting')

    votes = list(jury.votes.all()) if jury else []
    poster_weight_sum = sum(v.weight for v in votes if v.choice == 'poster')
    taker_weight_sum = sum(v.weight for v in votes if v.choice == 'taker')

    context = {
        'dispute': dispute,
        'task': task,
        'jury': jury,
        'juror': juror,
        'can_vote': can_vote,
        'votes': votes,
        'poster_weight_sum': poster_weight_sum,
        'taker_weight_sum': taker_weight_sum,
        'is_participant': is_participant,
        'is_juror': is_juror,
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

            # Form and assign decentralized jury
            create_jury_for_dispute(dispute)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Jury assigned.")
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
        if hasattr(dispute, 'jury'):
            refund_juror_stakes_on_withdrawal(dispute.jury)

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
def cast_vote_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not hasattr(dispute, 'jury'):
        messages.error(request, "No active jury exists for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    jury = dispute.jury
    juror = Juror.objects.filter(jury=jury, user=request.user).first()
    if not juror:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if juror.status != 'assigned':
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    reasoning = request.POST.get('reasoning', '')

    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice. Please select Poster Wins or Taker Wins.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        cast_juror_vote(juror, choice, reasoning)
        messages.success(request, f"Your vote for {'Poster' if choice == 'poster' else 'Taker'} has been recorded successfully.")
    except Exception as e:
        messages.error(request, f"Failed to record vote: {str(e)}")

    return redirect('dispute_detail', dispute_id=dispute.id)
