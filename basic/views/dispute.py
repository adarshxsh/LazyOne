import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import (
    Dispute, Task, Notification, DisputeJury, DisputeVote,
    RewardLedger, Friendship
)


def select_dispute_jury(dispute, num_jurors=5):
    task = dispute.task
    poster = task.posted_by
    worker = task.taken_by

    excluded_user_ids = {poster.id}
    if worker:
        excluded_user_ids.add(worker.id)

    # Exclude poster's direct friends
    if hasattr(poster, 'userprofile'):
        for f in poster.userprofile.friends.all():
            excluded_user_ids.add(f.user.id)
    for fs in Friendship.objects.filter(from_user__user=poster):
        excluded_user_ids.add(fs.to_user.user.id)
    for fs in Friendship.objects.filter(to_user__user=poster):
        excluded_user_ids.add(fs.from_user.user.id)

    # Exclude worker's direct friends
    if worker:
        if hasattr(worker, 'userprofile'):
            for f in worker.userprofile.friends.all():
                excluded_user_ids.add(f.user.id)
        for fs in Friendship.objects.filter(from_user__user=worker):
            excluded_user_ids.add(fs.to_user.user.id)
        for fs in Friendship.objects.filter(to_user__user=worker):
            excluded_user_ids.add(fs.from_user.user.id)

    eligible_users = list(User.objects.exclude(id__in=excluded_user_ids).filter(is_active=True))

    pool_size = min(num_jurors, len(eligible_users))
    if pool_size > 0 and pool_size % 2 == 0:
        pool_size -= 1  # Ensure odd number if possible

    selected_jurors = random.sample(eligible_users, pool_size) if pool_size > 0 else []

    jury = DisputeJury.objects.create(dispute=dispute)
    if selected_jurors:
        jury.jurors.set(selected_jurors)
        for juror in selected_jurors:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been selected as a juror for task dispute: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
    return jury


def check_and_resolve_dispute(dispute):
    if dispute.status != 'open':
        return

    task = dispute.task
    total_jurors = dispute.jury.jurors.count() if hasattr(dispute, 'jury') else 0

    poster_votes = dispute.votes.filter(vote='posted_by').count()
    worker_votes = dispute.votes.filter(vote='taken_by').count()

    majority_needed = (total_jurors // 2) + 1 if total_jurors > 0 else 1

    now = timezone.now()
    is_expired = now >= (dispute.created_at + timedelta(hours=72))

    resolved = False
    winning_side = None

    if poster_votes >= majority_needed:
        resolved = True
        winning_side = 'posted_by'
    elif worker_votes >= majority_needed:
        resolved = True
        winning_side = 'taken_by'
    elif is_expired:
        resolved = True
        if poster_votes > worker_votes:
            winning_side = 'posted_by'
        elif worker_votes > poster_votes:
            winning_side = 'taken_by'
        else:
            winning_side = 'split'

    if not resolved:
        return

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.save()

        if winning_side == 'taken_by':
            task.status = 'completed'
            task.save()
            if task.taken_by and hasattr(task.taken_by, 'userprofile'):
                worker_profile = task.taken_by.userprofile
                worker_profile.rewards += task.reward
                worker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_payout',
                    description=f"Dispute resolved in favor of worker: Payout for task '{task.title}'"
                )
            if task.posted_by:
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' was resolved in favor of worker ({task.taken_by.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in your favor! {task.reward} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        elif winning_side == 'posted_by':
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
                    transaction_type='dispute_refund',
                    description=f"Dispute resolved in favor of poster: Refund for task '{task.title}'"
                )
            if task.posted_by:
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' was resolved in your favor! {task.reward} points refunded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in favor of task poster ({task.posted_by.username}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        elif winning_side == 'split':
            task.status = 'cancelled'
            task.save()
            half_reward = task.reward // 2
            remainder = task.reward - half_reward

            if task.posted_by and hasattr(task.posted_by, 'userprofile'):
                p_profile = task.posted_by.userprofile
                p_profile.rewards += half_reward
                p_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=half_reward,
                    transaction_type='dispute_split',
                    description=f"Dispute expired - equal split refund for task: '{task.title}'"
                )
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' expired and was split equally. {half_reward} points returned.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            if task.taken_by and hasattr(task.taken_by, 'userprofile'):
                w_profile = task.taken_by.userprofile
                w_profile.rewards += remainder
                w_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=remainder,
                    transaction_type='dispute_split',
                    description=f"Dispute expired - equal split payout for task: '{task.title}'"
                )
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' expired and was split equally. {remainder} points awarded.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    check_and_resolve_dispute(dispute)

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = hasattr(dispute, 'jury') and request.user in dispute.jury.jurors.all()
    is_staff = request.user.is_staff

    if not is_participant and not is_juror and not is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    has_voted = False
    user_vote = None
    if is_juror:
        v = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()
        if v:
            has_voted = True
            user_vote = v.vote

    total_jurors = dispute.jury.jurors.count() if hasattr(dispute, 'jury') else 0
    votes_cast = dispute.votes.count()
    poster_votes = 0
    worker_votes = 0

    if dispute.status == 'resolved' or is_staff:
        poster_votes = dispute.votes.filter(vote='posted_by').count()
        worker_votes = dispute.votes.filter(vote='taken_by').count()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'total_jurors': total_jurors,
        'votes_cast': votes_cast,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'is_resolved': dispute.status == 'resolved',
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    if hasattr(task, 'dispute'):
        if task.dispute.status == 'open':
            return redirect('dispute_detail', dispute_id=task.dispute.id)
        else:
            task.dispute.delete()

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

        select_dispute_jury(dispute)

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully and assigned to community jury.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def vote_on_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open' or task.status != 'disputed':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not hasattr(dispute, 'jury') or request.user not in dispute.jury.jurors.all():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast your vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['posted_by', 'taken_by']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote=vote_choice
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

