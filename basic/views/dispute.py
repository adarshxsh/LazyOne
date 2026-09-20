from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, DisputeVote, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    votes = dispute.votes.all()
    total_votes_count = votes.count()
    poster_votes_count = votes.filter(voted_for=task.posted_by).count()
    taker_votes_count = votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    quorum_target = 3
    quorum_percentage = min(100, int((total_votes_count / quorum_target) * 100))

    poster_percentage = int((poster_votes_count / total_votes_count) * 100) if total_votes_count > 0 else 0
    taker_percentage = int((taker_votes_count / total_votes_count) * 100) if total_votes_count > 0 else 0

    user_vote = None
    if request.user.is_authenticated:
        user_vote = votes.filter(voter=request.user).first()

    is_participant = (request.user == task.posted_by) or (task.taken_by and request.user == task.taken_by)
    can_vote = request.user.is_authenticated and (not is_participant) and (dispute.status == 'open') and (user_vote is None)

    context = {
        'dispute': dispute,
        'task': task,
        'total_votes_count': total_votes_count,
        'poster_votes_count': poster_votes_count,
        'taker_votes_count': taker_votes_count,
        'quorum_target': quorum_target,
        'quorum_percentage': quorum_percentage,
        'poster_percentage': poster_percentage,
        'taker_percentage': taker_percentage,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)

def evaluate_consensus(dispute):
    """
    Evaluates current jury votes for a dispute.
    If total votes >= 3 and a clear majority (>50%) is reached, resolves the dispute.
    Must be called within an atomic transaction.
    """
    if dispute.status != 'open':
        return False

    task = dispute.task
    votes = dispute.votes.all()
    total_votes = votes.count()
    if total_votes < 3:
        return False

    poster_votes = votes.filter(voted_for=task.posted_by).count()
    taker_votes = votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    threshold = total_votes / 2.0

    if poster_votes > threshold:
        # Poster wins!
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'cancelled'
        task.save()

        # Refund task reward to poster
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Refund for cancelled task via dispute consensus: '{task.title}'"
        )

        # Deposit bond handling
        if dispute.raised_by == task.taken_by:
            dispute.forfeit_deposit(
                reason_description=f"Security deposit bond forfeited for dispute on task: '{task.title}'"
            )
        elif dispute.raised_by == task.posted_by:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for dispute on task: '{task.title}'"
            )

        # Notifications
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Community jury resolved dispute for task '{task.title}' in your favor.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Community jury resolved dispute for task '{task.title}' in favor of {task.posted_by.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        return True

    elif taker_votes > threshold and task.taken_by:
        # Taker wins!
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'completed'
        task.save()

        # Transfer task reward to taker
        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += task.reward
        taker_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=task.reward,
            transaction_type='task_completion',
            description=f"Reward awarded for completed task via dispute consensus: '{task.title}'"
        )

        # Deposit bond handling
        if dispute.raised_by == task.taken_by:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for resolved dispute on task: '{task.title}'"
            )
        elif dispute.raised_by == task.posted_by:
            dispute.forfeit_deposit(
                beneficiary=task.taken_by,
                reason_description=f"Security deposit bond forfeited for dispute on task: '{task.title}'"
            )

        # Notifications
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Community jury resolved dispute for task '{task.title}' in favor of {task.taken_by.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Community jury resolved dispute for task '{task.title}' in your favor.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        return True

    return False

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or (task.taken_by and request.user == task.taken_by):
        messages.error(request, "Task participants are not allowed to vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    feedback = request.POST.get('feedback', '').strip()

    if not voted_for_id:
        messages.error(request, "Please select a participant to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    target_user = None
    if str(voted_for_id) == str(task.posted_by.id) or voted_for_id == 'posted_by':
        target_user = task.posted_by
    elif task.taken_by and (str(voted_for_id) == str(task.taken_by.id) or voted_for_id == 'taken_by'):
        target_user = task.taken_by

    if not target_user:
        messages.error(request, "Invalid vote target.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
            messages.error(request, "You have already cast your vote on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=target_user,
            feedback=feedback
        )

        resolved = evaluate_consensus(dispute)
        if resolved:
            messages.success(request, f"Your vote has been submitted! Consensus was reached and dispute for '{task.title}' has been resolved.")
        else:
            messages.success(request, "Your vote has been recorded successfully.")

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
