import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse

from ..models import (
    Dispute, Task, Notification, RewardLedger,
    UserProfile, Friendship, FriendRequest,
    JuryPool, JurorAssignment
)


def select_jurors_for_dispute(dispute, target_size=3):
    task = dispute.task
    participants = {task.posted_by, task.taken_by, dispute.raised_by}
    excluded_user_ids = {u.id for u in participants if u}

    for participant in list(participants):
        if not participant:
            continue
        profile = getattr(participant, 'userprofile', None)
        if profile:
            # 1. UserProfile.friends
            friend_user_ids = profile.friends.values_list('user_id', flat=True)
            excluded_user_ids.update(friend_user_ids)

            # 2. Friendship (both directions)
            fs_to_user_ids = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            fs_from_user_ids = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(fs_to_user_ids)
            excluded_user_ids.update(fs_from_user_ids)

        # 3. FriendRequest (both directions)
        fr_to_user_ids = FriendRequest.objects.filter(from_user=participant).values_list('to_user_id', flat=True)
        fr_from_user_ids = FriendRequest.objects.filter(to_user=participant).values_list('from_user_id', flat=True)
        excluded_user_ids.update(fr_to_user_ids)
        excluded_user_ids.update(fr_from_user_ids)

    candidate_users = list(User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids))

    if len(candidate_users) < target_size:
        jury_pool, _ = JuryPool.objects.update_or_create(
            dispute=dispute,
            defaults={'target_size': target_size, 'status': 'insufficient_jurors'}
        )
        staff_users = User.objects.filter(is_staff=True)
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"Dispute #{dispute.id} for task '{task.title}' requires staff escalation (insufficient jurors available).",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        return jury_pool

    selected_jurors = random.sample(candidate_users, target_size)
    jury_pool, _ = JuryPool.objects.update_or_create(
        dispute=dispute,
        defaults={'target_size': target_size, 'status': 'assigned'}
    )
    jury_pool.assignments.all().delete()
    for juror in selected_jurors:
        JurorAssignment.objects.create(jury_pool=jury_pool, juror=juror)
        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a peer juror for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
    return jury_pool


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    jury_pool = getattr(dispute, 'jury_pool', None)

    is_juror = False
    user_assignment = None
    if jury_pool:
        user_assignment = jury_pool.assignments.filter(juror=request.user).first()
        if user_assignment:
            is_juror = True

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    poster_votes = 0
    worker_votes = 0
    total_votes = 0
    if jury_pool:
        poster_votes = jury_pool.assignments.filter(vote='poster').count()
        worker_votes = jury_pool.assignments.filter(vote='worker').count()
        total_votes = poster_votes + worker_votes

    context = {
        'dispute': dispute,
        'task': task,
        'jury_pool': jury_pool,
        'is_juror': is_juror,
        'user_assignment': user_assignment,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'total_votes': total_votes,
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

        panel_size = 3
        panel_size_param = request.POST.get('panel_size') or request.POST.get('target_size')
        if panel_size_param:
            try:
                parsed_size = int(panel_size_param)
                if parsed_size in [3, 5]:
                    panel_size = parsed_size
            except ValueError:
                pass

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

            select_jurors_for_dispute(dispute, target_size=panel_size)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')


def _resolve_dispute_by_jury(dispute, jury_pool, winner):
    task = dispute.task
    dispute.status = 'resolved'
    dispute.save()

    jury_pool.status = 'resolved'
    jury_pool.save()

    if winner == 'poster':
        dispute.forfeit_deposit(
            beneficiary=task.posted_by,
            reason_description=f"Juror majority vote resolved dispute in favor of poster on task '{task.title}'"
        )
        task.status = 'cancelled'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute on task '{task.title}' was resolved in your favor by peer jurors.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute on task '{task.title}' was resolved in favor of poster by peer jurors.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    elif winner == 'worker':
        dispute.refund_deposit(
            reason_description=f"Juror majority vote resolved dispute in favor of worker on task '{task.title}'"
        )
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
                description=f"Reward points for task '{task.title}' awarded via juror dispute resolution"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute on task '{task.title}' was resolved in your favor by peer jurors.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute on task '{task.title}' was resolved in favor of worker by peer jurors.",
            link=reverse('dispute_detail', args=[dispute.id])
        )


@login_required(login_url='/login/')
@require_POST
def submit_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    jury_pool = getattr(dispute, 'jury_pool', None)
    if not jury_pool or jury_pool.status != 'assigned':
        messages.error(request, "Jury pool is not active for voting on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = JurorAssignment.objects.filter(jury_pool=jury_pool, juror=request.user).first()
    if not assignment:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if assignment.vote is not None:
        messages.error(request, "You have already submitted your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'worker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        assignment.vote = vote_choice
        assignment.voted_at = timezone.now()
        assignment.save()

        poster_votes = jury_pool.assignments.filter(vote='poster').count()
        worker_votes = jury_pool.assignments.filter(vote='worker').count()
        majority_threshold = (jury_pool.target_size // 2) + 1

        if poster_votes >= majority_threshold:
            _resolve_dispute_by_jury(dispute, jury_pool, winner='poster')
            messages.success(request, "Vote recorded. Majority vote reached: dispute resolved in favor of the poster.")
        elif worker_votes >= majority_threshold:
            _resolve_dispute_by_jury(dispute, jury_pool, winner='worker')
            messages.success(request, "Vote recorded. Majority vote reached: dispute resolved in favor of the worker.")
        else:
            messages.success(request, "Your vote has been submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)


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

        if hasattr(dispute, 'jury_pool') and dispute.jury_pool:
            dispute.jury_pool.status = 'resolved'
            dispute.jury_pool.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
