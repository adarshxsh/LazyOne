from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, JuryPool, DisputeVote, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

def check_and_resolve_dispute(dispute):
    if dispute.status != 'open':
        return

    if not hasattr(dispute, 'jury_pool'):
        return

    total_jurors = dispute.jury_pool.jurors.count()
    if total_jurors == 0:
        return

    majority_threshold = (total_jurors // 2) + 1
    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()
    total_votes = poster_votes + taker_votes

    winning_decision = None
    if poster_votes >= majority_threshold:
        winning_decision = 'poster'
    elif taker_votes >= majority_threshold:
        winning_decision = 'taker'
    elif total_votes >= total_jurors or timezone.now() >= dispute.created_at + timedelta(hours=72):
        if poster_votes > taker_votes:
            winning_decision = 'poster'
        elif taker_votes > poster_votes:
            winning_decision = 'taker'
        else:
            winning_decision = 'poster'

    if not winning_decision:
        return

    with transaction.atomic():
        task = dispute.task
        dispute.status = 'resolved'
        dispute.save()

        if winning_decision == 'taker':
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
                description=f"Dispute resolved in favor of taker: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points awarded.",
                link=reverse('my_tasks')
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in favor of the taker.",
                link=reverse('my_tasks')
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
                description=f"Dispute resolved in favor of poster: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points refunded.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of the poster.",
                    link=reverse('my_tasks')
                )

        for juror in dispute.jury_pool.jurors.all():
            Notification.objects.create(
                recipient=juror,
                message=f"Arbitration completed for task '{task.title}'. Result: In favor of {winning_decision.capitalize()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_juror = hasattr(dispute, 'jury_pool') and dispute.jury_pool.jurors.filter(id=request.user.id).exists()
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    if dispute.status == 'open':
        check_and_resolve_dispute(dispute)
        dispute.refresh_from_db()

    total_jurors = dispute.jury_pool.jurors.count() if hasattr(dispute, 'jury_pool') else 0
    votes = dispute.votes.all()
    votes_cast = votes.count()
    poster_votes = votes.filter(vote='poster').count()
    taker_votes = votes.filter(vote='taker').count()
    user_vote = votes.filter(juror=request.user).first() if is_juror else None

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'total_jurors': total_jurors,
        'votes_cast': votes_cast,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'user_vote': user_vote,
        'votes': votes,
    }
    return render(request, 'dispute_detail.html', context)


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

        # Spawn JuryPool
        JuryPool.create_for_dispute(dispute)

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
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    is_juror = hasattr(dispute, 'jury_pool') and dispute.jury_pool.jurors.filter(id=request.user.id).exists()
    if not is_juror:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_decision = request.POST.get('vote')
    if vote_decision not in ['poster', 'taker']:
        messages.error(request, "Invalid vote decision.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    rationale = request.POST.get('rationale', '').strip()
    DisputeVote.objects.create(
        dispute=dispute,
        juror=request.user,
        vote=vote_decision,
        rationale=rationale
    )
    messages.success(request, "Your vote has been submitted successfully.")

    check_and_resolve_dispute(dispute)
    return redirect('dispute_detail', dispute_id=dispute.id)


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

