import logging
import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, JuryAssignment, RewardLedger, UserProfile, Friendship, FriendRequest

logger = logging.getLogger(__name__)

def select_and_stake_jurors(dispute, panel_size=3):
    """
    Dynamically samples candidate jurors from active user accounts whenever a dispute is raised.
    Filters out direct friends, friend request contacts, counterparties (posted_by, taken_by, raised_by),
    users with active disputes, and users without the minimum required reward point balance.
    Locks the required reward point stake for each selected juror in escrow upon panel assignment.
    """
    task = dispute.task
    required_stake = max(100, int(task.reward * 0.10))

    # Task counterparties and dispute raiser
    dispute_parties_users = set()
    for user in [task.posted_by, task.taken_by, dispute.raised_by]:
        if user:
            dispute_parties_users.add(user)

    dispute_party_ids = {u.id for u in dispute_parties_users}
    excluded_user_ids = set(dispute_party_ids)

    # Social graph exclusions: direct friends and pending/accepted friend request contacts
    for p_id in dispute_party_ids:
        # UserProfile.friends
        p_profile = UserProfile.objects.filter(user_id=p_id).first()
        if p_profile:
            friend_user_ids = set(p_profile.friends.values_list('user_id', flat=True))
            excluded_user_ids.update(friend_user_ids)
            reverse_friend_user_ids = set(UserProfile.objects.filter(friends=p_profile).values_list('user_id', flat=True))
            excluded_user_ids.update(reverse_friend_user_ids)

        # Friendship relationships
        friendship_user_ids_from = Friendship.objects.filter(from_user__user_id=p_id).values_list('to_user__user_id', flat=True)
        friendship_user_ids_to = Friendship.objects.filter(to_user__user_id=p_id).values_list('from_user__user_id', flat=True)
        excluded_user_ids.update(friendship_user_ids_from)
        excluded_user_ids.update(friendship_user_ids_to)

        # FriendRequest contacts (both from_user and to_user)
        fr_sent = FriendRequest.objects.filter(from_user_id=p_id).values_list('to_user_id', flat=True)
        fr_recv = FriendRequest.objects.filter(to_user_id=p_id).values_list('from_user_id', flat=True)
        excluded_user_ids.update(fr_sent)
        excluded_user_ids.update(fr_recv)

    # Active open dispute exclusions
    open_disputes = Dispute.objects.filter(status='open')
    for open_disp in open_disputes:
        if open_disp.raised_by_id:
            excluded_user_ids.add(open_disp.raised_by_id)
        if open_disp.task.posted_by_id:
            excluded_user_ids.add(open_disp.task.posted_by_id)
        if open_disp.task.taken_by_id:
            excluded_user_ids.add(open_disp.task.taken_by_id)

    # Active jury assignment exclusions (users already assigned as jurors on open disputes)
    active_juror_ids = JuryAssignment.objects.filter(
        dispute__status='open',
        status='assigned'
    ).values_list('user_id', flat=True)
    excluded_user_ids.update(active_juror_ids)

    # Candidate query: Active users with sufficient reward balance, excluding all excluded_user_ids
    candidate_qs = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=required_stake
    ).exclude(id__in=excluded_user_ids)

    candidates = list(candidate_qs)

    # Audit event & notification if candidates < panel_size (Requirement 6)
    if len(candidates) < panel_size:
        logger.warning(
            f"Audit Event: Insufficient neutral candidates for dispute #{dispute.id} on task '{task.title}'. "
            f"Required: {panel_size}, Available: {len(candidates)}"
        )
        staff_users = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True))
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"Audit Alert: Insufficient neutral candidates available for dispute #{dispute.id} on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    selected_users = random.sample(candidates, min(len(candidates), panel_size))

    for juror in selected_users:
        with transaction.atomic():
            assignment, created = JuryAssignment.objects.get_or_create(
                dispute=dispute,
                user=juror,
                defaults={'staked_amount': required_stake, 'status': 'assigned'}
            )
            if created:
                juror_profile = UserProfile.objects.get(user=juror)
                juror_profile.rewards -= required_stake
                juror_profile.save()

                RewardLedger.objects.create(
                    user=juror,
                    task=task,
                    amount=-required_stake,
                    transaction_type='juror_stake',
                    description=f"Locked stake for dispute #{dispute.id} on task '{task.title}'"
                )

                Notification.objects.create(
                    recipient=juror,
                    message=f"You have been selected as a juror for dispute on task '{task.title}'. A stake of {required_stake} points has been reserved.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    return len(selected_users)

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = JuryAssignment.objects.filter(dispute=dispute, user=request.user).exists()
    is_participant = request.user in [task.posted_by, task.taken_by]

    if not is_participant and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    assignments = dispute.jury_assignments.all() if (is_participant or request.user.is_staff or is_juror) else []

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'assignments': assignments,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
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

            counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            select_and_stake_jurors(dispute)

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

        assignments = dispute.jury_assignments.filter(status='assigned')
        for assignment in assignments:
            juror = assignment.user
            juror_profile = UserProfile.objects.get(user=juror)
            juror_profile.rewards += assignment.staked_amount
            juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=assignment.staked_amount,
                transaction_type='juror_release',
                description=f"Released stake for withdrawn dispute #{dispute.id}"
            )
            assignment.status = 'released'
            assignment.save()

        task.status = 'in_progress'
        task.save()

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
