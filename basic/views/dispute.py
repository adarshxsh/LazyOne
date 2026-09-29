import random
import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import (
    Dispute, Task, Notification, RewardLedger, UserProfile,
    Friendship, FriendRequest, DisputeJuror, DisputeVote
)

logger = logging.getLogger(__name__)


def select_and_assign_jurors(dispute):
    """
    Selects a random, friend-isolated juror pool for an open dispute.
    Strictly excludes litigants, 1st-degree direct friends, and users with task history.
    Assigns an odd-numbered panel (3 or 5 members) and creates notifications.
    """
    if dispute.jurors.exists():
        return list(dispute.jurors.all())

    task = dispute.task
    litigant_ids = set()
    if task.posted_by_id:
        litigant_ids.add(task.posted_by_id)
    if task.taken_by_id:
        litigant_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        litigant_ids.add(dispute.raised_by_id)

    excluded_user_ids = set(litigant_ids)

    # Exclude 1st-degree direct friends of litigants
    for litigant_id in litigant_ids:
        try:
            profile = UserProfile.objects.get(user_id=litigant_id)
            # ManyToMany field friends
            m2m_friends = profile.friends.values_list('user_id', flat=True)
            excluded_user_ids.update(m2m_friends)

            # Friendship model ties
            fs_to = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            fs_from = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(fs_to)
            excluded_user_ids.update(fs_from)

            # FriendRequest ties
            fr_to = FriendRequest.objects.filter(from_user_id=litigant_id).values_list('to_user_id', flat=True)
            fr_from = FriendRequest.objects.filter(to_user_id=litigant_id).values_list('from_user_id', flat=True)
            excluded_user_ids.update(fr_to)
            excluded_user_ids.update(fr_from)
        except UserProfile.DoesNotExist:
            pass

        # Exclude task history connections with litigants
        taken_history = Task.objects.filter(posted_by_id=litigant_id, taken_by__isnull=False).values_list('taken_by_id', flat=True)
        excluded_user_ids.update(taken_history)

        posted_history = Task.objects.filter(taken_by_id=litigant_id).values_list('posted_by_id', flat=True)
        excluded_user_ids.update(posted_history)

    # Candidate pool query excluding litigants, friends, and staff
    candidate_qs = User.objects.exclude(id__in=excluded_user_ids).filter(is_active=True, is_staff=False)
    candidate_users = list(candidate_qs)
    num_candidates = len(candidate_users)

    # Select odd-numbered panel (3 or 5 members)
    if num_candidates >= 5:
        target_size = 5
    elif num_candidates >= 3:
        target_size = 3
    elif num_candidates >= 1:
        target_size = 1
    else:
        target_size = 0

    if target_size > 0:
        selected_users = random.sample(candidate_users, target_size)
    else:
        selected_users = []

    assigned_jurors = []
    for user in selected_users:
        dj, _ = DisputeJuror.objects.get_or_create(dispute=dispute, user=user)
        assigned_jurors.append(dj)
        Notification.objects.create(
            recipient=user,
            message=f"You have been assigned as a juror for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    logger.info(f"Dispute {dispute.id}: Assigned {len(assigned_jurors)} jurors out of {num_candidates} candidates.")
    return assigned_jurors


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_litigant = (request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by)
    is_assigned_juror = DisputeJuror.objects.filter(dispute=dispute, user=request.user).exists()
    is_staff = request.user.is_staff

    if not (is_litigant or is_staff or is_assigned_juror):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_has_voted = DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists()
    assigned_jurors_count = dispute.jurors.count()
    total_votes_cast = dispute.votes.count()
    deliberation_finished = (assigned_jurors_count > 0 and total_votes_cast >= assigned_jurors_count) or (dispute.status == 'resolved')

    # Litigants should not see individual juror identities or votes until deliberation finishes
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count() if deliberation_finished or is_staff else None
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if (deliberation_finished or is_staff) and task.taken_by else None

    context = {
        'dispute': dispute,
        'task': task,
        'is_litigant': is_litigant,
        'is_assigned_juror': is_assigned_juror,
        'is_staff': is_staff,
        'user_has_voted': user_has_voted,
        'assigned_jurors_count': assigned_jurors_count,
        'total_votes_cast': total_votes_cast,
        'deliberation_finished': deliberation_finished,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
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

            # Assign random friend-isolated community jurors
            assigned_jurors = select_and_assign_jurors(dispute)

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. A community jury ({len(assigned_jurors)} jurors) has been assigned.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. {len(assigned_jurors)} jurors assigned.")
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


@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if not DisputeJuror.objects.filter(dispute=dispute, user=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.info(request, "You have already cast your vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id or int(voted_for_id) not in [task.posted_by_id, task.taken_by_id]:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            voted_for=voted_for_user
        )

        assigned_jurors_count = dispute.jurors.count()
        votes_count = dispute.votes.count()

        # If all assigned jurors have voted, finalize deliberation!
        if votes_count >= assigned_jurors_count:
            poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
            taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

            if taker_votes > poster_votes:
                winner = task.taken_by
                # Taker wins dispute: complete task, transfer reward to taker, refund deposit bond to taker
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded upon winning jury dispute for task: '{task.title}'"
                )
                task_doer_profile = task.taken_by.userprofile
                task_doer_profile.rewards += task.reward
                task_doer_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=task.reward,
                    transaction_type='task_completion', description=f"Completed task via jury resolution: '{task.title}'"
                )
                task.status = 'completed'
                dispute.status = 'resolved'
                task.save()
                dispute.save()
            else:
                winner = task.posted_by
                # Poster wins dispute: forfeit taker deposit to poster, reset task or mark resolved
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Taker deposit bond forfeited to poster upon losing jury dispute for task: '{task.title}'"
                )
                dispute.status = 'resolved'
                task.status = 'in_progress'
                dispute.save()
                task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Jury deliberation finished for dispute on task '{task.title}'. Dispute resolved in favor of {winner.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Jury deliberation finished for dispute on task '{task.title}'. Dispute resolved in favor of {winner.username}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    messages.success(request, "Your confidential vote has been cast.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def staff_resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can override dispute resolution.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    winner_id = request.POST.get('winner')
    if not winner_id or int(winner_id) not in [task.posted_by_id, task.taken_by_id]:
        messages.error(request, "Invalid winner selection for staff override.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner = get_object_or_404(User, id=winner_id)

    with transaction.atomic():
        if winner == task.taken_by:
            dispute.refund_deposit(
                reason_description=f"Deposit bond refunded upon staff override for task: '{task.title}'"
            )
            task_doer_profile = task.taken_by.userprofile
            task_doer_profile.rewards += task.reward
            task_doer_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.reward,
                transaction_type='task_completion', description=f"Completed task via staff override: '{task.title}'"
            )
            task.status = 'completed'
            dispute.status = 'resolved'
            task.save()
            dispute.save()
        else:
            dispute.forfeit_deposit(
                beneficiary=task.posted_by,
                reason_description=f"Taker deposit bond forfeited to poster upon staff override for task: '{task.title}'"
            )
            dispute.status = 'resolved'
            task.status = 'in_progress'
            dispute.save()
            task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' has been resolved by staff in favor of {winner.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' has been resolved by staff in favor of {winner.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Staff override applied. Dispute resolved in favor of {winner.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)
