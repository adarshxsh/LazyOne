import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JuryPool, JurorAssignment, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse


def create_jury_pool(dispute, pool_size=5, required_stake=20):
    """
    Instantiates a JuryPool for a dispute and assigns neutral, active platform users.
    Excludes the task poster, task taker, and users with insufficient rewards.
    """
    task = dispute.task
    excluded_user_ids = [task.posted_by.id]
    if task.taken_by:
        excluded_user_ids.append(task.taken_by.id)

    eligible_users = list(User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=required_stake
    ).exclude(id__in=excluded_user_ids))

    selected_count = min(len(eligible_users), pool_size)
    if selected_count > 0:
        selected_users = random.sample(eligible_users, selected_count)
    else:
        selected_users = []

    jury_pool, created = JuryPool.objects.get_or_create(
        dispute=dispute,
        defaults={
            'pool_size': pool_size,
            'required_stake': required_stake,
            'status': 'active'
        }
    )

    for user in selected_users:
        assignment, _ = JurorAssignment.objects.get_or_create(
            jury_pool=jury_pool,
            user=user,
            defaults={
                'status': 'assigned',
                'is_active': True,
                'assigned_at': timezone.now()
            }
        )
        Notification.objects.create(
            recipient=user,
            message=f"You have been assigned as a juror for the dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return jury_pool


def check_and_replace_inactive_jurors(dispute):
    """
    Replaces assigned jurors who have not voted within 48 hours.
    """
    if not hasattr(dispute, 'jury_pool') or dispute.jury_pool.status != 'active':
        return

    jury_pool = dispute.jury_pool
    cutoff_time = timezone.now() - timedelta(hours=48)
    inactive_assignments = JurorAssignment.objects.filter(
        jury_pool=jury_pool,
        status='assigned',
        is_active=True,
        assigned_at__lte=cutoff_time
    )

    if not inactive_assignments.exists():
        return

    task = dispute.task
    all_assigned_user_ids = list(JurorAssignment.objects.filter(
        jury_pool=jury_pool
    ).values_list('user_id', flat=True))

    excluded_user_ids = set([task.posted_by.id] + ([task.taken_by.id] if task.taken_by else []) + all_assigned_user_ids)

    for assignment in inactive_assignments:
        old_user = assignment.user
        assignment.status = 'replaced'
        assignment.is_active = False
        assignment.save()

        eligible_users = list(User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=jury_pool.required_stake
        ).exclude(id__in=excluded_user_ids))

        if eligible_users:
            new_user = random.choice(eligible_users)
            excluded_user_ids.add(new_user.id)

            JurorAssignment.objects.create(
                jury_pool=jury_pool,
                user=new_user,
                assigned_at=timezone.now(),
                status='assigned',
                is_active=True
            )

            Notification.objects.create(
                recipient=new_user,
                message=f"You have been assigned as a replacement juror for the dispute on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        Notification.objects.create(
            recipient=old_user,
            message=f"You were replaced as a juror for task: '{task.title}' due to inactivity.",
            link=reverse('home')
        )


def process_dispute_consensus(dispute, winning_user, losing_user):
    """
    Settles task escrow, refunds/forfeits dispute deposit bonds,
    and distributes juror stake refunds and reward shares upon consensus.
    """
    task = dispute.task
    jury_pool = dispute.jury_pool

    if jury_pool.status != 'active' or dispute.status != 'open':
        return

    jury_pool.status = 'resolved'
    jury_pool.save()

    dispute.status = 'resolved'

    loser_deposit = 0

    if winning_user == task.taken_by:
        # Taker wins dispute
        task.status = 'completed'
        task.save()

        task_doer_profile = task.taken_by.userprofile
        task_doer_profile.rewards += task.reward
        task_doer_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=task.reward,
            transaction_type='task_completion',
            description=f"Task reward awarded upon dispute consensus: '{task.title}'"
        )

        if dispute.raised_by == task.taken_by:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for dispute won on task: '{task.title}'"
            )
        else:
            loser_deposit = dispute.deposit_amount
            dispute.forfeit_deposit(
                reason_description=f"Security deposit bond forfeited for dispute lost on task: '{task.title}'"
            )

    else:
        # Poster wins dispute
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
            description=f"Refund for task reward upon dispute consensus: '{task.title}'"
        )

        if dispute.raised_by == task.posted_by:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for dispute won on task: '{task.title}'"
            )
        else:
            loser_deposit = dispute.deposit_amount
            dispute.forfeit_deposit(
                reason_description=f"Security deposit bond forfeited for dispute lost on task: '{task.title}'"
            )

    dispute.save()

    # Process Majority Juror Refunds & Rewards
    majority_votes = DisputeVote.objects.filter(jury_pool=jury_pool, vote_for=winning_user)
    majority_count = majority_votes.count()

    bonus_per_juror = (loser_deposit // majority_count) if (loser_deposit > 0 and majority_count > 0) else 0

    for vote in majority_votes:
        juror = vote.juror
        juror_profile = juror.userprofile

        # Refund stake
        juror_profile.rewards += vote.stake_amount
        RewardLedger.objects.create(
            user=juror,
            task=task,
            amount=vote.stake_amount,
            transaction_type='juror_refund',
            description=f"Juror stake refunded for dispute on task: '{task.title}'"
        )

        # Share of loser deposit
        if bonus_per_juror > 0:
            juror_profile.rewards += bonus_per_juror
            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=bonus_per_juror,
                transaction_type='juror_reward',
                description=f"Juror bonus reward share for dispute on task: '{task.title}'"
            )

        juror_profile.save()

        Notification.objects.create(
            recipient=juror,
            message=f"Dispute for task '{task.title}' resolved in favor of your vote ({winning_user.username}). Your stake was refunded.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    # Notify Minority Jurors
    minority_votes = DisputeVote.objects.filter(jury_pool=jury_pool).exclude(vote_for=winning_user)
    for vote in minority_votes:
        Notification.objects.create(
            recipient=vote.juror,
            message=f"Dispute for task '{task.title}' reached consensus for {winning_user.username}. Your stake was forfeited.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    # Notify Task Parties
    Notification.objects.create(
        recipient=winning_user,
        message=f"Dispute for task '{task.title}' was resolved in your favor by peer jury consensus.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    if losing_user:
        Notification.objects.create(
            recipient=losing_user,
            message=f"Dispute for task '{task.title}' was resolved against you by peer jury consensus.",
            link=reverse('dispute_detail', args=[dispute.id])
        )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    check_and_replace_inactive_jurors(dispute)

    is_assigned_juror = False
    user_assignment = None
    has_voted = False

    if hasattr(dispute, 'jury_pool'):
        user_assignment = JurorAssignment.objects.filter(
            jury_pool=dispute.jury_pool,
            user=request.user,
            is_active=True
        ).first()
        if user_assignment:
            is_assigned_juror = True
            has_voted = DisputeVote.objects.filter(
                jury_pool=dispute.jury_pool,
                juror=request.user
            ).exists()

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    context = {
        'dispute': dispute,
        'task': task,
        'is_assigned_juror': is_assigned_juror,
        'user_assignment': user_assignment,
        'has_voted': has_voted,
        'jury_pool': getattr(dispute, 'jury_pool', None)
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

            create_jury_pool(dispute)

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
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open' or not hasattr(dispute, 'jury_pool') or dispute.jury_pool.status != 'active':
        messages.error(request, "This dispute is not open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    jury_pool = dispute.jury_pool
    assignment = JurorAssignment.objects.filter(
        jury_pool=jury_pool,
        user=request.user,
        is_active=True,
        status='assigned'
    ).first()

    if not assignment:
        messages.error(request, "You are not an active assigned juror on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(jury_pool=jury_pool, juror=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_param = request.POST.get('vote') or request.POST.get('vote_for')
    task = dispute.task

    target_user = None
    if vote_param == 'poster' or (vote_param and str(vote_param) == str(task.posted_by.id)):
        target_user = task.posted_by
    elif vote_param == 'taker' or (vote_param and task.taken_by and str(vote_param) == str(task.taken_by.id)):
        target_user = task.taken_by

    if not target_user:
        messages.error(request, "Invalid vote target. You must vote for either the task poster or task taker.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    required_stake = jury_pool.required_stake
    user_profile = request.user.userprofile

    if user_profile.rewards < required_stake:
        messages.error(request, f"Insufficient balance. You need at least {required_stake} points to stake a vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= required_stake
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-required_stake,
            transaction_type='juror_stake',
            description=f"Stake bond for voting on dispute on task: '{task.title}'"
        )

        DisputeVote.objects.create(
            jury_pool=jury_pool,
            juror=request.user,
            vote_for=target_user,
            stake_amount=required_stake
        )

        assignment.status = 'voted'
        assignment.save()

        votes_for_poster = DisputeVote.objects.filter(jury_pool=jury_pool, vote_for=task.posted_by).count()
        votes_for_taker = DisputeVote.objects.filter(jury_pool=jury_pool, vote_for=task.taken_by).count() if task.taken_by else 0

        pool_size = jury_pool.pool_size
        threshold = (pool_size // 2) + 1

        if votes_for_poster >= threshold:
            process_dispute_consensus(dispute, winning_user=task.posted_by, losing_user=task.taken_by)
            messages.success(request, f"Vote submitted successfully! Consensus reached in favor of {task.posted_by.username}.")
        elif votes_for_taker >= threshold:
            process_dispute_consensus(dispute, winning_user=task.taken_by, losing_user=task.posted_by)
            messages.success(request, f"Vote submitted successfully! Consensus reached in favor of {task.taken_by.username}.")
        else:
            messages.success(request, f"Vote submitted successfully! Staked {required_stake} points.")

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

        if hasattr(dispute, 'jury_pool'):
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
