import math
import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Count
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import (
    Dispute, Task, Notification, RewardLedger,
    UserProfile, Friendship, FriendRequest, JuryAssignment
)


def get_direct_friend_user_ids(user):
    """
    Returns a set of user IDs for all direct friends of `user` across:
    - UserProfile.friends (bidirectional ManyToMany)
    - Friendship model (from_user / to_user)
    - FriendRequest model (accepted from_user / to_user)
    """
    if not user:
        return set()
    friend_ids = set()
    try:
        profile = user.userprofile
        friend_ids.update(profile.friends.values_list('user__id', flat=True))
        friend_ids.update(User.objects.filter(userprofile__friends=profile).values_list('id', flat=True))

        friend_ids.update(Friendship.objects.filter(from_user=profile).values_list('to_user__user__id', flat=True))
        friend_ids.update(Friendship.objects.filter(to_user=profile).values_list('from_user__user__id', flat=True))
    except UserProfile.DoesNotExist:
        pass

    friend_ids.update(FriendRequest.objects.filter(from_user=user, is_accepted=True).values_list('to_user__id', flat=True))
    friend_ids.update(FriendRequest.objects.filter(to_user=user, is_accepted=True).values_list('from_user__id', flat=True))

    friend_ids.discard(user.id)
    return friend_ids


def get_eligible_juror_candidates(task, required_stake):
    """
    Returns QuerySet of eligible User objects for jury selection:
    1. Excludes task poster, task taker, and their direct friends.
    2. Excludes unverified profiles (phone or instagram must be verified).
    3. Excludes users with < 3 completed tasks (taken or posted).
    4. Excludes users with available rewards < required_stake.
    """
    excluded_user_ids = {task.posted_by.id}
    if task.taken_by:
        excluded_user_ids.add(task.taken_by.id)

    excluded_user_ids.update(get_direct_friend_user_ids(task.posted_by))
    if task.taken_by:
        excluded_user_ids.update(get_direct_friend_user_ids(task.taken_by))

    candidates = User.objects.exclude(id__in=excluded_user_ids).filter(is_active=True)

    candidates = candidates.filter(
        Q(userprofile__is_phone_verified=True) | Q(userprofile__is_instagram_verified=True)
    )

    candidates = candidates.filter(userprofile__rewards__gte=required_stake)

    candidates = candidates.annotate(
        completed_task_count=Count('taken_tasks', filter=Q(taken_tasks__status='completed')) +
                             Count('posted_tasks', filter=Q(posted_tasks__status='completed'))
    ).filter(completed_task_count__gte=3)

    return candidates


def select_and_stake_jurors(dispute, panel_size=3):
    """
    Selects up to `panel_size` qualified neutral jurors, locks required reward stake
    from each juror, creates JuryAssignment records, and logs RewardLedger entries.
    Must be called inside transaction.atomic().
    """
    required_stake = dispute.calculate_juror_stake()

    existing_assignments = dispute.jury_assignments.all()
    if existing_assignments.exists():
        existing_assignments.delete()

    candidates = list(get_eligible_juror_candidates(dispute.task, required_stake))
    if not candidates:
        return []

    selected_jurors = random.sample(candidates, min(panel_size, len(candidates)))

    for candidate in selected_jurors:
        profile = UserProfile.objects.select_for_update().get(user=candidate)
        profile.rewards -= required_stake
        profile.save()

        JuryAssignment.objects.create(
            dispute=dispute,
            juror=candidate,
            staked_amount=required_stake,
            status='assigned'
        )

        RewardLedger.objects.create(
            user=candidate,
            task=dispute.task,
            amount=-required_stake,
            transaction_type='juror_stake',
            description=f"Juror stake locked for dispute on task: '{dispute.task.title}'"
        )

        Notification.objects.create(
            recipient=candidate,
            message=f"You have been selected as a juror for dispute on task: '{dispute.task.title}'. Stake of {required_stake} points locked.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return selected_jurors


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_assigned_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    context = {
        'dispute': dispute,
        'task': task
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

            select_and_stake_jurors(dispute)

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
        dispute.resolve_jury_stakes()
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
