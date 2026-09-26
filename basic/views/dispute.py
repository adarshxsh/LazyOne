import math
from datetime import timedelta
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JurorAssignment
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Check for counter-bond SLA expiration
    dispute.check_counter_bond_sla()

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = JurorAssignment.objects.filter(dispute=dispute, juror=request.user).exists()
    
    # Allow participants, assigned jurors, staff, or open dispute viewers (potential jurors)
    if not is_participant and not is_juror and not request.user.is_staff and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    poster_votes = JurorAssignment.objects.filter(dispute=dispute, has_voted=True, vote_choice='poster').count()
    taker_votes = JurorAssignment.objects.filter(dispute=dispute, has_voted=True, vote_choice='taker').count()
    total_votes = poster_votes + taker_votes

    user_assignment = JurorAssignment.objects.filter(dispute=dispute, juror=request.user).first()
    has_voted = user_assignment.has_voted if user_assignment else False

    is_poster = (request.user == task.posted_by)
    is_taker = (request.user == task.taken_by)
    can_vote = (not is_participant) and (dispute.status == 'open') and (dispute.poster_escrow_status == 'held') and (dispute.worker_escrow_status == 'held') and (not has_voted)

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'user_assignment': user_assignment,
        'has_voted': has_voted,
        'is_poster': is_poster,
        'is_taker': is_taker,
        'can_vote': can_vote,
        'juror_stake_amount': getattr(settings, 'JUROR_STAKE_AMOUNT', 25),
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

        counter_bond_deadline = timezone.now() + timedelta(hours=24)

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
                dispute.worker_deposit_amount = deposit_amount
                dispute.worker_escrow_status = 'held'
                dispute.poster_deposit_amount = deposit_amount
                dispute.poster_escrow_status = 'pending'
                dispute.counter_bond_deadline = counter_bond_deadline
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    worker_deposit_amount=deposit_amount,
                    worker_escrow_status='held',
                    poster_deposit_amount=deposit_amount,
                    poster_escrow_status='pending',
                    counter_bond_deadline=counter_bond_deadline
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
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. A matching deposit bond of {deposit_amount} points is required within 24 hours.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Task poster must match the bond within 24 hours.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def post_counter_bond(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by:
        messages.error(request, "Only the task poster can post a matching counter-bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.check_counter_bond_sla()
    if dispute.status != 'open' or dispute.poster_escrow_status != 'pending':
        messages.error(request, "Matching deposit bond is not pending for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    required_bond = dispute.poster_deposit_amount or task.deposit_bond_amount
    poster_profile = request.user.userprofile

    if poster_profile.rewards < required_bond:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {required_bond} points to match the deposit bond, but you only have {poster_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        poster_profile.rewards -= required_bond
        poster_profile.save()

        dispute.poster_deposit_amount = required_bond
        dispute.poster_escrow_status = 'held'
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-required_bond,
            transaction_type='dispute_poster_deposit',
            description=f"Matching deposit bond held for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.taken_by,
            message=f"{request.user.username} has posted the matching deposit bond for task: '{task.title}'. Dispute is now active for jury review.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Matching deposit bond of {required_bond} points posted successfully. Dispute is now active for jury review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    dispute.check_counter_bond_sla()
    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.poster_escrow_status != 'held' or dispute.worker_escrow_status != 'held':
        messages.error(request, "Task poster has not matched the deposit bond yet. Jury deliberation cannot begin.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user in [task.posted_by, task.taken_by]:
        messages.error(request, "Task participants cannot vote as jurors on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    existing_vote = JurorAssignment.objects.filter(dispute=dispute, juror=request.user, has_voted=True).first()
    if existing_vote:
        messages.error(request, "You have already cast your vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice')
    if vote_choice not in ['poster', 'taker', 'worker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    if vote_choice == 'worker':
        vote_choice = 'taker'

    stake_amount = getattr(settings, 'JUROR_STAKE_AMOUNT', 25)
    juror_profile = request.user.userprofile

    if juror_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {stake_amount} points to stake as a juror, but you only have {juror_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_profile.rewards -= stake_amount
        juror_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake locked for vote on dispute for task: '{task.title}'"
        )

        assignment, _ = JurorAssignment.objects.get_or_create(dispute=dispute, juror=request.user)
        assignment.has_voted = True
        assignment.vote_choice = vote_choice
        assignment.stake_amount = stake_amount
        assignment.stake_status = 'held'
        assignment.voted_at = timezone.now()
        assignment.save()

        check_consensus_and_settle(dispute)

    messages.success(request, f"Your vote as a juror has been recorded. {stake_amount} points locked as stake.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def check_consensus_and_settle(dispute):
    task = dispute.task
    poster_votes = JurorAssignment.objects.filter(dispute=dispute, has_voted=True, vote_choice='poster').count()
    taker_votes = JurorAssignment.objects.filter(dispute=dispute, has_voted=True, vote_choice='taker').count()
    total_votes = poster_votes + taker_votes

    # Consensus reached if 2 or more matching votes, or at least 3 total votes cast
    winning_choice = None
    if poster_votes >= 2:
        winning_choice = 'poster'
    elif taker_votes >= 2:
        winning_choice = 'taker'
    elif total_votes >= 3:
        winning_choice = 'poster' if poster_votes > taker_votes else ('taker' if taker_votes > poster_votes else None)

    if winning_choice:
        with transaction.atomic():
            if winning_choice == 'taker':
                winning_user = task.taken_by
                losing_user = task.posted_by
                winning_deposit = dispute.worker_deposit_amount
                losing_deposit = dispute.poster_deposit_amount
            else:
                winning_user = task.posted_by
                losing_user = task.taken_by
                winning_deposit = dispute.poster_deposit_amount
                losing_deposit = dispute.worker_deposit_amount

            # 1. Refund winning party deposit bond
            if winning_user:
                win_profile = winning_user.userprofile
                win_profile.rewards += winning_deposit
                win_profile.save()
                RewardLedger.objects.create(
                    user=winning_user,
                    task=task,
                    amount=winning_deposit,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )

            # 2. Winning task outcome
            if winning_choice == 'taker' and winning_user:
                win_profile = winning_user.userprofile
                win_profile.rewards += task.reward
                win_profile.save()
                RewardLedger.objects.create(
                    user=winning_user,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Task reward awarded for winning dispute on task: '{task.title}'"
                )
                task.status = 'completed'
                dispute.worker_escrow_status = 'refunded'
                dispute.poster_escrow_status = 'forfeited'
            elif winning_choice == 'poster' and winning_user:
                win_profile = winning_user.userprofile
                win_profile.rewards += task.reward
                win_profile.save()
                RewardLedger.objects.create(
                    user=winning_user,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Task reward refunded for winning dispute on task: '{task.title}'"
                )
                task.status = 'cancelled'
                dispute.poster_escrow_status = 'refunded'
                dispute.worker_escrow_status = 'forfeited'

            task.save()

            # 3. Forfeit losing party deposit bond
            if losing_user:
                RewardLedger.objects.create(
                    user=losing_user,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                )

            # 4. Jury Slashing and Reward Pool Redistribution
            majority_assignments = JurorAssignment.objects.filter(dispute=dispute, has_voted=True, vote_choice=winning_choice)
            minority_assignments = JurorAssignment.objects.filter(dispute=dispute, has_voted=True).exclude(vote_choice=winning_choice)

            slashed_stakes_sum = 0
            for min_assignment in minority_assignments:
                min_assignment.stake_status = 'slashed'
                min_assignment.save()
                slashed_stakes_sum += min_assignment.stake_amount
                RewardLedger.objects.create(
                    user=min_assignment.juror,
                    task=task,
                    amount=0,
                    transaction_type='juror_slash',
                    description=f"Juror stake slashed for unaligned vote on dispute for task: '{task.title}'"
                )

            reward_pool = losing_deposit + slashed_stakes_sum
            maj_count = majority_assignments.count()
            per_juror_reward = math.floor(reward_pool / maj_count) if maj_count > 0 else 0

            for maj_assignment in majority_assignments:
                juror_profile = maj_assignment.juror.userprofile
                # Refund initial stake
                juror_profile.rewards += maj_assignment.stake_amount
                RewardLedger.objects.create(
                    user=maj_assignment.juror,
                    task=task,
                    amount=maj_assignment.stake_amount,
                    transaction_type='juror_stake_refund',
                    description=f"Juror stake refunded for consensus vote on dispute for task: '{task.title}'"
                )
                # Pay pro-rata share of consensus reward pool
                if per_juror_reward > 0:
                    juror_profile.rewards += per_juror_reward
                    RewardLedger.objects.create(
                        user=maj_assignment.juror,
                        task=task,
                        amount=per_juror_reward,
                        transaction_type='juror_reward',
                        description=f"Consensus jury reward distributed for dispute on task: '{task.title}'"
                    )
                juror_profile.save()
                maj_assignment.stake_status = 'refunded'
                maj_assignment.save()

            dispute.status = 'resolved'
            dispute.consensus_outcome = winning_choice
            dispute.escrow_status = 'refunded'
            dispute.save()

            # Notifications
            dispute_link = reverse('dispute_detail', args=[dispute.id])
            for maj_assignment in majority_assignments:
                Notification.objects.create(
                    recipient=maj_assignment.juror,
                    message=f"You earned {per_juror_reward} reward points for voting with consensus on dispute '{task.title}'.",
                    link=dispute_link
                )

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

