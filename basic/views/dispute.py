import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, JuryMember, UserProfile, Friendship, RewardLedger

JURY_REWARD_BONUS = 50

def select_neutral_jurors(dispute):
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    exclude_user_ids = {poster.id}
    if taker:
        exclude_user_ids.add(taker.id)

    # 1. Direct friends of poster
    try:
        poster_profile = poster.userprofile
        exclude_user_ids.update(poster_profile.friends.values_list('user__id', flat=True))
    except UserProfile.DoesNotExist:
        pass

    poster_fs1 = Friendship.objects.filter(from_user__user=poster).values_list('to_user__user__id', flat=True)
    poster_fs2 = Friendship.objects.filter(to_user__user=poster).values_list('from_user__user__id', flat=True)
    exclude_user_ids.update(poster_fs1)
    exclude_user_ids.update(poster_fs2)

    # 2. Direct friends of taker
    if taker:
        try:
            taker_profile = taker.userprofile
            exclude_user_ids.update(taker_profile.friends.values_list('user__id', flat=True))
        except UserProfile.DoesNotExist:
            pass

        taker_fs1 = Friendship.objects.filter(from_user__user=taker).values_list('to_user__user__id', flat=True)
        taker_fs2 = Friendship.objects.filter(to_user__user=taker).values_list('from_user__user__id', flat=True)
        exclude_user_ids.update(taker_fs1)
        exclude_user_ids.update(taker_fs2)

    # 3. Task participation history with poster
    posted_tasks_with_poster = Task.objects.filter(posted_by=poster).exclude(taken_by__isnull=True).values_list('taken_by__id', flat=True)
    taken_tasks_with_poster = Task.objects.filter(taken_by=poster).values_list('posted_by__id', flat=True)
    exclude_user_ids.update(posted_tasks_with_poster)
    exclude_user_ids.update(taken_tasks_with_poster)

    # 4. Task participation history with taker
    if taker:
        posted_tasks_with_taker = Task.objects.filter(posted_by=taker).exclude(taken_by__isnull=True).values_list('taken_by__id', flat=True)
        taken_tasks_with_taker = Task.objects.filter(taken_by=taker).values_list('posted_by__id', flat=True)
        exclude_user_ids.update(posted_tasks_with_taker)
        exclude_user_ids.update(taken_tasks_with_taker)

    eligible_users = list(User.objects.filter(is_active=True).exclude(id__in=exclude_user_ids))

    sample_size = min(5, len(eligible_users))
    if sample_size > 0:
        selected_users = random.sample(eligible_users, sample_size)
        for u in selected_users:
            JuryMember.objects.create(dispute=dispute, user=u)
            Notification.objects.create(
                recipient=u,
                message=f"You have been selected as a jury member for a dispute on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

def _resolve_dispute(dispute, winning_party):
    task = dispute.task
    dispute.status = 'resolved'
    dispute.winning_party = winning_party
    dispute.resolved_at = timezone.now()

    if winning_party == 'poster':
        dispute.winner = task.posted_by
        dispute.save()

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
            description=f"Refund for dispute resolved in your favor: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' was resolved in your favor. {task.reward} points refunded.",
            link=reverse('my_tasks')
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of the task poster.",
                link=reverse('my_tasks')
            )

    elif winning_party == 'taker':
        dispute.winner = task.taken_by
        dispute.save()

        task.status = 'completed'
        task.save()

        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += task.reward
        taker_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=task.reward,
            transaction_type='task_completion',
            description=f"Reward for dispute resolved in your favor: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for task '{task.title}' was resolved in your favor. {task.reward} points awarded.",
            link=reverse('my_tasks')
        )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' was resolved in favor of the task taker.",
            link=reverse('my_tasks')
        )

    # Reward winning majority jurors
    winning_jurors = dispute.jurors.filter(vote=winning_party)
    for juror_member in winning_jurors:
        juror_profile = juror_member.user.userprofile
        juror_profile.rewards += JURY_REWARD_BONUS
        juror_profile.save()

        RewardLedger.objects.create(
            user=juror_member.user,
            task=task,
            amount=JURY_REWARD_BONUS,
            transaction_type='jury_reward',
            description=f"Jury duty reward for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=juror_member.user,
            message=f"You received {JURY_REWARD_BONUS} bonus points for voting with the majority in dispute on task '{task.title}'.",
            link=reverse('rewards')
        )

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    dispute.check_and_escalate()

    is_poster = (request.user == task.posted_by)
    is_taker = (request.user == task.taken_by)
    juror_assignment = dispute.jurors.filter(user=request.user).first()
    is_juror = (juror_assignment is not None)
    is_staff = request.user.is_staff

    if not (is_poster or is_taker or is_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    context = {
        'dispute': dispute,
        'task': task,
        'is_poster': is_poster,
        'is_taker': is_taker,
        'is_juror': is_juror,
        'juror_assignment': juror_assignment,
        'total_jurors': dispute.jurors.count(),
        'votes_submitted': dispute.jurors.filter(vote__isnull=False).count(),
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
        with transaction.atomic():
            dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
            task.status = 'disputed'
            task.save()
            select_neutral_jurors(dispute)

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, "Dispute raised successfully. Neutral peer jury has been selected.")
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

@login_required(login_url='/login/')
@require_POST
def submit_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    dispute.check_and_escalate()

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror_assignment = dispute.jurors.filter(user=request.user).first()
    if not juror_assignment:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if juror_assignment.vote is not None:
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_assignment.vote = vote_choice
        juror_assignment.voted_at = timezone.now()
        juror_assignment.save()

        poster_votes = dispute.jurors.filter(vote='poster').count()
        taker_votes = dispute.jurors.filter(vote='taker').count()
        total_jurors = dispute.jurors.count()
        majority_threshold = (total_jurors // 2) + 1 if total_jurors > 0 else 3

        if poster_votes >= majority_threshold:
            _resolve_dispute(dispute, winning_party='poster')
            messages.success(request, "Your vote has been cast. Simple majority reached: Dispute resolved in favor of the Task Poster.")
        elif taker_votes >= majority_threshold:
            _resolve_dispute(dispute, winning_party='taker')
            messages.success(request, "Your vote has been cast. Simple majority reached: Dispute resolved in favor of the Task Taker.")
        else:
            messages.success(request, "Your vote has been submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)
