from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    if not is_participant and not request.user.is_staff and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = None
    if request.user.is_authenticated:
        user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()

    poster_votes = dispute.votes.filter(vote='poster').count()
    worker_votes = dispute.votes.filter(vote='worker').count()
    total_votes = dispute.votes.count()

    voting_window_active = (timezone.now() <= dispute.created_at + timedelta(days=7))
    can_vote = (not is_participant and dispute.status == 'open' and user_vote is None and voting_window_active)

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'total_votes': total_votes,
        'is_participant': is_participant,
        'can_vote': can_vote,
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
        worker_profile = request.user.userprofile
        poster_profile = task.posted_by.userprofile

        if worker_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {worker_profile.rewards} points."
            )
            return redirect('my_tasks')

        if poster_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Task poster has insufficient reward points balance for deposit bond ({poster_profile.rewards} points available, {deposit_amount} required)."
            )
            return redirect('my_tasks')

        with transaction.atomic():
            worker_profile.rewards -= deposit_amount
            worker_profile.save()

            poster_profile.rewards -= deposit_amount
            poster_profile.save()

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
            RewardLedger.objects.create(
                user=task.posted_by,
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
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond from both worker and poster.")
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
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Deposit bonds have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and worker cannot vote as jurors on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if timezone.now() > dispute.created_at + timedelta(days=7):
        messages.error(request, "Voting window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    STAKE_AMOUNT = 10
    voter_profile = request.user.userprofile
    if voter_profile.rewards < STAKE_AMOUNT:
        messages.error(
            request,
            f"Insufficient reward points balance to stake as a juror. You need at least {STAKE_AMOUNT} points, but you have {voter_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'worker']:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = task.posted_by if vote_choice == 'poster' else task.taken_by

    with transaction.atomic():
        voter_profile.rewards -= STAKE_AMOUNT
        voter_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote=vote_choice,
            voted_for=voted_for_user,
            stake_amount=STAKE_AMOUNT
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-STAKE_AMOUNT,
            transaction_type='juror_stake',
            description=f"Stake locked to vote on dispute for task: '{task.title}'"
        )

    messages.success(request, f"Vote submitted successfully! {STAKE_AMOUNT} points staked.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status == 'resolved':
        messages.info(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_choice = request.POST.get('winner')
    if not winner_choice and dispute.votes.exists():
        poster_votes = dispute.votes.filter(vote='poster').count()
        worker_votes = dispute.votes.filter(vote='worker').count()
        if poster_votes > worker_votes:
            winner_choice = 'poster'
        elif worker_votes > poster_votes:
            winner_choice = 'worker'
        else:
            winner_choice = 'worker' if dispute.raised_by == dispute.task.taken_by else 'poster'
    elif not winner_choice:
        winner_choice = 'worker' if dispute.raised_by == dispute.task.taken_by else 'poster'

    if winner_choice not in ['poster', 'worker']:
        messages.error(request, "Invalid winner selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.resolve(winner_choice)
    messages.success(request, f"Dispute resolved in favor of {winner_choice.capitalize()}. Deposits and juror rewards distributed.")
    return redirect('dispute_detail', dispute_id=dispute.id)
