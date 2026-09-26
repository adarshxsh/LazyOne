from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    poster_votes = dispute.poster_votes_count
    taker_votes = dispute.taker_votes_count
    total_votes = dispute.total_votes_count
    quorum = dispute.QUORUM

    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    user_vote = dispute.votes.filter(voter=request.user).first()
    has_voted = (user_vote is not None)

    consensus_percentage = min(100, int((total_votes / quorum) * 100)) if quorum > 0 else 0

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'quorum': quorum,
        'is_party': is_party,
        'user_vote': user_vote,
        'has_voted': has_voted,
        'consensus_percentage': consensus_percentage,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and task takers cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_param = request.POST.get('voted_for') or request.POST.get('voted_for_id') or request.POST.get('vote')
    voted_for_user = None

    if voted_for_param:
        if str(voted_for_param) == 'poster' or str(voted_for_param) == str(task.posted_by.id) or str(voted_for_param) == task.posted_by.username:
            voted_for_user = task.posted_by
        elif str(voted_for_param) == 'taker' or (task.taken_by and (str(voted_for_param) == str(task.taken_by.id) or str(voted_for_param) == task.taken_by.username)):
            voted_for_user = task.taken_by

    if not voted_for_user:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
            messages.error(request, "You have already voted on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for_user
        )

        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        taker_votes = dispute.votes.filter(voted_for=task.taken_by).count()
        total_votes = poster_votes + taker_votes
        quorum = dispute.QUORUM

        if total_votes >= quorum:
            if taker_votes > poster_votes:
                _resolve_dispute(dispute, winner=task.taken_by, loser=task.posted_by)
                messages.success(request, f"Vote submitted! Quorum reached. Dispute resolved in favor of {task.taken_by.username}.")
            elif poster_votes > taker_votes:
                _resolve_dispute(dispute, winner=task.posted_by, loser=task.taken_by)
                messages.success(request, f"Vote submitted! Quorum reached. Dispute resolved in favor of {task.posted_by.username}.")
            else:
                messages.success(request, "Vote submitted! Quorum reached, but vote is tied. Waiting for additional votes.")
        else:
            messages.success(request, f"Vote submitted! Current tally: {total_votes}/{quorum} votes.")

    return redirect('dispute_detail', dispute_id=dispute.id)


def _resolve_dispute(dispute, winner, loser):
    task = dispute.task

    if winner == task.taken_by:
        taker_profile = winner.userprofile
        taker_profile.rewards += task.reward
        taker_profile.save()

        RewardLedger.objects.create(
            user=winner,
            task=task,
            amount=task.reward,
            transaction_type='task_completion',
            description=f"Completed task: '{task.title}'"
        )
        task.status = 'completed'
        task.save()

        if dispute.raised_by == winner:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon dispute consensus resolution for task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=winner,
                reason_description=f"Security deposit bond forfeited upon dispute consensus resolution for task: '{task.title}'"
            )
    else:
        poster_profile = winner.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=winner,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Refund for cancelled task: '{task.title}'"
        )
        task.status = 'cancelled'
        task.save()

        if dispute.raised_by == winner:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon dispute consensus resolution for task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=winner,
                reason_description=f"Security deposit bond forfeited upon dispute consensus resolution for task: '{task.title}'"
            )

    dispute.status = 'resolved'
    dispute.save()

    Notification.objects.create(
        recipient=task.posted_by,
        message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username} by community consensus.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    if task.taken_by:
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username} by community consensus.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

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
