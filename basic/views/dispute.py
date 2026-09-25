from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

MIN_VOTE_QUORUM = 5

def check_and_settle_dispute(dispute):
    """
    Tallies votes for the given dispute. If total votes reach at least MIN_VOTE_QUORUM (5),
    settles the dispute automatically based on majority consensus.
    """
    if dispute.status != 'open':
        return False

    task = dispute.task
    total_votes = dispute.votes.count()
    if total_votes < MIN_VOTE_QUORUM:
        return False

    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    worker_votes = dispute.votes.filter(voted_for=task.taken_by).count()

    with transaction.atomic():
        if worker_votes > poster_votes:
            # Worker wins
            task.status = 'completed'
            task.save()

            if task.taken_by:
                worker_profile = task.taken_by.userprofile
                worker_profile.rewards += task.reward
                worker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task via community dispute settlement: '{task.title}'"
                )

            # Handle deposit bond
            if task.taken_by and dispute.raised_by == task.taken_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                )
            elif task.taken_by:
                dispute.forfeit_deposit(
                    beneficiary=task.taken_by,
                    reason_description=f"Security deposit bond forfeited to worker upon dispute settlement for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            # Notifications
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of worker ({task.taken_by.username if task.taken_by else 'Worker'}) by community jury.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in your favor by community jury. {task.reward} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            return True

        elif poster_votes > worker_votes:
            # Poster wins
            task.status = 'cancelled'
            task.save()

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for cancelled task via community dispute settlement: '{task.title}'"
            )

            # Handle deposit bond
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited to poster upon dispute settlement for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            # Notifications
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor by community jury. Task points refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in favor of poster ({task.posted_by.username}) by community jury.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            return True

    return False

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    total_votes = dispute.votes.count()
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    worker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    user_vote = dispute.votes.filter(voter=request.user).first()
    is_participant = (request.user in [task.posted_by, task.taken_by])
    can_vote = (not is_participant) and (dispute.status == 'open') and (user_vote is None)
    vote_history = dispute.votes.select_related('voter', 'voted_for').order_by('-created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
        'vote_history': vote_history,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and task workers cannot vote on their own disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    valid_ids = [str(task.posted_by.id)]
    if task.taken_by:
        valid_ids.append(str(task.taken_by.id))

    if not voted_for_id or str(voted_for_id) not in valid_ids:
        messages.error(request, "Please select a valid candidate to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)
    comment = request.POST.get('comment', '').strip()

    with transaction.atomic():
        vote = DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for_user,
            comment=comment
        )
        settled = check_and_settle_dispute(dispute)

    if settled:
        messages.success(request, "Your vote was recorded and consensus quorum was reached! The dispute has been settled.")
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
