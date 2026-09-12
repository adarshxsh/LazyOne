from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, JuryAssignment, DisputeVote, RewardLedger, UserProfile
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    user_vote = dispute.votes.filter(juror=request.user).first()
    has_voted = user_vote is not None
    required_quorum = dispute.get_required_quorum()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'required_quorum': required_quorum,
        'total_votes': dispute.total_votes,
        'poster_votes': dispute.poster_votes,
        'taker_votes': dispute.taker_votes,
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
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice') or request.POST.get('vote') or request.POST.get('voted_for')
    reason = request.POST.get('reason', '')

    voted_for = None
    choice = None

    if vote_choice in ['poster', 'posted_by', str(task.posted_by.id)]:
        voted_for = task.posted_by
        choice = 'poster'
    elif vote_choice in ['taker', 'taken_by', str(task.taken_by.id)]:
        voted_for = task.taken_by
        choice = 'taker'
    else:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JuryAssignment.objects.get_or_create(dispute=dispute, juror=request.user)
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            voted_for=voted_for,
            vote_choice=choice,
            reason=reason
        )
        settled = check_and_settle_dispute(dispute)

    if settled:
        messages.success(request, "Your vote was recorded. Voting quorum was reached and the dispute has been resolved!")
    else:
        messages.success(request, "Your vote has been recorded.")

    return redirect('dispute_detail', dispute_id=dispute.id)

def check_and_settle_dispute(dispute):
    if dispute.status != 'open':
        return False

    task = dispute.task
    required_quorum = dispute.get_required_quorum()
    poster_votes = dispute.poster_votes
    taker_votes = dispute.taker_votes
    total_votes = dispute.total_votes

    majority_needed = (required_quorum // 2) + 1

    winner = None
    if poster_votes >= majority_needed:
        winner = task.posted_by
    elif taker_votes >= majority_needed:
        winner = task.taken_by
    elif total_votes >= required_quorum:
        if poster_votes > taker_votes:
            winner = task.posted_by
        elif taker_votes > poster_votes:
            winner = task.taken_by

    if winner:
        with transaction.atomic():
            dispute.status = 'resolved'
            dispute.winner = winner
            dispute.resolved_at = timezone.now()
            dispute.save()

            if winner == task.taken_by:
                task.status = 'completed'
                task.save()

                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_payout',
                    description=f"Dispute settlement payout for task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of worker ({task.taken_by.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            else:
                task.status = 'cancelled'
                task.save()

                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_refund',
                    description=f"Dispute settlement refund for task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points refunded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of task poster ({task.posted_by.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        return True
    return False

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')
