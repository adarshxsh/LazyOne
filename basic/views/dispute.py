from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    votes = dispute.votes.all()
    worker_votes_count = votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
    poster_votes_count = votes.filter(voted_for=task.posted_by).count()
    user_vote = votes.filter(voter=request.user).first()
    is_party = (request.user == task.posted_by or request.user == task.taken_by)

    context = {
        'dispute': dispute,
        'task': task,
        'votes': votes,
        'worker_votes_count': worker_votes_count,
        'poster_votes_count': poster_votes_count,
        'user_vote': user_vote,
        'is_party': is_party,
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
                dispute.poster_deposit_amount = 0
                dispute.poster_escrow_status = 'pending'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    poster_deposit_amount=0,
                    poster_escrow_status='pending'
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
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. Counter-stake bond is required to activate community jury review.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Task poster must match counter-stake before community jury review.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def counter_stake_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user != task.posted_by:
        messages.error(request, "Only the task poster can counter-stake this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.poster_escrow_status == 'held':
        messages.info(request, "You have already counter-staked this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile

    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to match the dispute, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        dispute.poster_deposit_amount = deposit_amount
        dispute.poster_escrow_status = 'held'
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type='dispute_deposit',
            description=f"Counter-stake deposit bond held for dispute on task: '{task.title}'"
        )

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"{request.user.username} has matched the deposit bond for task '{task.title}'. Community jury review is now active.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Counter-stake successful! {deposit_amount} points held as deposit bond. Community jury review is now active.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if not dispute.is_jury_review_active:
        messages.error(request, "Disputes cannot proceed to community jury review until both parties commit required deposit bonds.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and taker cannot vote as community jurors.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a party to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for = get_object_or_404(User, id=voted_for_id)
    if voted_for not in [task.posted_by, task.taken_by]:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    stake_amount = dispute.juror_stake_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward balance. You need at least {stake_amount} points to stake a vote, but you have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= stake_amount
        user_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for,
            stake_amount=stake_amount,
            status='held'
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake bond held for vote on dispute for task: '{task.title}'"
        )

    messages.success(request, f"Your vote for {voted_for.username} has been recorded! {stake_amount} points held as juror stake bond.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def settle_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to settle this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_id = request.POST.get('winner')
    winner = None
    if winner_id:
        winner = get_object_or_404(User, id=winner_id)
        if winner not in [task.posted_by, task.taken_by]:
            messages.error(request, "Invalid winner selection.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.settle(winner=winner)
    messages.success(request, f"Dispute for '{task.title}' has been settled successfully.")
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

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Held deposit bonds have been refunded.")
    return redirect('my_tasks')
