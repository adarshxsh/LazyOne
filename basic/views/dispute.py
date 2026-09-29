from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

def resolve_dispute_with_winner(dispute, winner):
    """
    Settles a dispute in favor of `winner` (either task.taken_by or task.posted_by).
    Executes reward distribution, escrow bond settlement, status updates, and notifications atomically.
    """
    task = dispute.task
    with transaction.atomic():
        if winner == task.taken_by:
            task.status = 'completed'
            task.save()
            if task.taken_by and hasattr(task.taken_by, 'userprofile'):
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward awarded for dispute resolved in favor of taker on task: '{task.title}'"
                )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded on dispute resolved in favor of taker for task '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=f"Security deposit bond forfeited on dispute resolved in favor of taker for task '{task.title}'"
                    )
        else:
            task.status = 'cancelled'
            task.save()
            if task.posted_by and hasattr(task.posted_by, 'userprofile'):
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Reward refunded for dispute resolved in favor of poster on task: '{task.title}'"
                )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded on dispute resolved in favor of poster for task '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Security deposit bond forfeited on dispute resolved in favor of poster for task '{task.title}'"
                    )

        dispute.status = 'resolved'
        dispute.save()

        # Send notifications to participants
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username}.",
            link=dispute_link
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username}.",
                link=dispute_link
            )

def check_and_resolve_dispute(dispute):
    """
    Checks if dispute has reached the quorum threshold and automatically resolves it if met.
    """
    if dispute.status != 'open':
        return False

    total_votes = dispute.votes.count()
    if total_votes >= dispute.quorum_threshold:
        poster_votes = dispute.votes.filter(voted_for=dispute.task.posted_by).count()
        taker_votes = dispute.votes.filter(voted_for=dispute.task.taken_by).count()

        if taker_votes > poster_votes:
            winner = dispute.task.taken_by
        else:
            winner = dispute.task.posted_by

        resolve_dispute_with_winner(dispute, winner)
        return True
    return False

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    # Restrict viewing resolved disputes to participants and staff only
    if dispute.status != 'open' and not is_participant and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    total_votes = dispute.votes.count()
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count()

    user_vote = dispute.votes.filter(voter=request.user).first()
    can_vote = (dispute.status == 'open' and not is_participant and user_vote is None)

    context = {
        'dispute': dispute,
        'task': task,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'quorum_threshold': dispute.quorum_threshold,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    target_param = request.POST.get('voted_for') or request.POST.get('voted_for_id')
    voted_for_user = None

    if target_param:
        target_str = str(target_param).strip()
        if target_str == str(task.posted_by.id) or target_str == 'poster':
            voted_for_user = task.posted_by
        elif task.taken_by and (target_str == str(task.taken_by.id) or target_str == 'taker'):
            voted_for_user = task.taken_by

    if not voted_for_user:
        messages.error(request, "Invalid vote decision.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for_user
        )

        resolved = check_and_resolve_dispute(dispute)
        if resolved:
            messages.success(request, "Your vote was recorded. Voting quorum was reached and the dispute has been resolved!")
        else:
            messages.success(request, "Your vote has been submitted successfully.")

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
