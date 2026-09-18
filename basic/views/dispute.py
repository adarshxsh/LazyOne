import logging
import random
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JuryAssignment, UserProfile, Friendship, FriendRequest
from django.views.decorators.http import require_POST
from django.urls import reverse

logger = logging.getLogger(__name__)

def get_excluded_user_ids(task, dispute=None):
    """
    Returns a set of User IDs that must be excluded from candidate juror pools
    due to direct involvement or social ties with task participants.
    """
    excluded_ids = set()
    if not task:
        return excluded_ids

    # 1. Direct task participants
    if task.posted_by_id:
        excluded_ids.add(task.posted_by_id)
    if task.taken_by_id:
        excluded_ids.add(task.taken_by_id)
    if dispute and dispute.raised_by_id:
        excluded_ids.add(dispute.raised_by_id)

    # 2. Helper to extract all friend user IDs for a given user
    def get_friend_user_ids(user):
        friend_ids = set()
        if not user:
            return friend_ids

        # UserProfile.friends ManyToMany
        if hasattr(user, 'userprofile'):
            profile = user.userprofile
            for friend_profile in profile.friends.all():
                if friend_profile.user_id:
                    friend_ids.add(friend_profile.user_id)
            for friend_profile in UserProfile.objects.filter(friends=profile):
                if friend_profile.user_id:
                    friend_ids.add(friend_profile.user_id)

            # Friendship model
            for fs in Friendship.objects.filter(from_user=profile):
                if fs.to_user and fs.to_user.user_id:
                    friend_ids.add(fs.to_user.user_id)
            for fs in Friendship.objects.filter(to_user=profile):
                if fs.from_user and fs.from_user.user_id:
                    friend_ids.add(fs.from_user.user_id)

        # FriendRequest model
        for fr in FriendRequest.objects.filter(from_user=user):
            if fr.to_user_id:
                friend_ids.add(fr.to_user_id)
        for fr in FriendRequest.objects.filter(to_user=user):
            if fr.from_user_id:
                friend_ids.add(fr.from_user_id)

        return friend_ids

    if task.posted_by:
        excluded_ids.update(get_friend_user_ids(task.posted_by))

    if task.taken_by:
        excluded_ids.update(get_friend_user_ids(task.taken_by))

    return excluded_ids


def select_juror_pool(dispute, panel_size=None):
    """
    Selects a panel of eligible, unbiased community members as jurors for a dispute.
    Excludes task participants and their direct social graph connections.
    If candidate pool is insufficient (< panel_size), gracefully escalates to staff resolution.
    """
    if panel_size is None:
        panel_size = getattr(settings, 'JUROR_PANEL_SIZE', 3)

    task = dispute.task
    excluded_ids = get_excluded_user_ids(task, dispute)

    # Exclude users already assigned as jurors for this dispute
    existing_juror_ids = JuryAssignment.objects.filter(dispute=dispute).values_list('juror_id', flat=True)
    excluded_ids.update(existing_juror_ids)

    # Filter eligible active users who are not superusers/staff or excluded
    eligible_candidates = list(
        User.objects.filter(is_active=True)
        .exclude(id__in=excluded_ids)
        .exclude(is_staff=True)
        .exclude(is_superuser=True)
    )

    logger.info(f"Dispute {dispute.id}: Found {len(eligible_candidates)} eligible juror candidates (panel_size={panel_size}).")

    if len(eligible_candidates) < panel_size:
        logger.warning(
            f"Dispute {dispute.id}: Insufficient eligible candidates ({len(eligible_candidates)} found, {panel_size} required). "
            f"Escalating dispute to staff resolution."
        )
        dispute.is_escalated_to_staff = True
        dispute.save()

        # Notify staff users
        staff_users = User.objects.filter(is_staff=True, is_active=True)
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"Dispute #{dispute.id} for task '{task.title}' escalated to staff resolution due to low juror candidate pool.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        return []

    # Random selection from eligible candidates
    selected_jurors = random.sample(eligible_candidates, panel_size)
    assignments = []
    for juror in selected_jurors:
        assignment = JuryAssignment.objects.create(dispute=dispute, juror=juror, status='assigned')
        assignments.append(assignment)
        logger.info(f"Dispute {dispute.id}: Assigned juror {juror.username} (ID: {juror.id}).")

        # Notify selected juror
        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a peer juror for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return assignments


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_staff = request.user.is_staff
    is_assigned_juror = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).exists()

    if not (is_participant or is_staff or is_assigned_juror):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    assigned_jurors = User.objects.filter(jury_assignments__dispute=dispute)

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_assigned_juror': is_assigned_juror,
        'assigned_jurors': assigned_jurors,
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
                dispute.is_escalated_to_staff = False
                dispute.save()
                JuryAssignment.objects.filter(dispute=dispute).delete()
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

            select_juror_pool(dispute)

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
