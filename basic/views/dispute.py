from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    dispute.check_counter_bond_timeout()

    task = dispute.task
    is_poster = (request.user == task.posted_by)
    is_worker = (request.user == task.taken_by or request.user == dispute.raised_by)

    worker_votes_count = dispute.votes.filter(vote='worker').count()
    poster_votes_count = dispute.votes.filter(vote='poster').count()
    user_vote = dispute.votes.filter(voter=request.user).first()

    can_counter_bond = (
        is_poster and
        dispute.status == 'open' and
        dispute.counter_bond_status == 'pending'
    )

    can_vote = (
        not is_poster and
        not is_worker and
        dispute.status == 'open' and
        dispute.counter_bond_status == 'posted' and
        user_vote is None
    )

    context = {
        'dispute': dispute,
        'task': task,
        'is_poster': is_poster,
        'is_worker': is_worker,
        'can_counter_bond': can_counter_bond,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'worker_votes_count': worker_votes_count,
        'poster_votes_count': poster_votes_count,
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

        deadline = timezone.now() + timedelta(hours=48)

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.counter_bond_status = 'pending'
                dispute.counter_bond_deadline = deadline
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    counter_bond_status='pending',
                    counter_bond_deadline=deadline,
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
                message=f"{request.user.username} has raised a dispute for task '{task.title}'. Please match the {deposit_amount}-point counter-bond within 48 hours.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Task poster has 48 hours to match counter-bond.")
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

    if dispute.check_counter_bond_timeout():
        messages.error(request, "The counter-bond response window has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open' or dispute.counter_bond_status != 'pending':
        messages.error(request, "Counter-bond cannot be posted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    required_amount = dispute.deposit_amount
    poster_profile = request.user.userprofile

    if poster_profile.rewards < required_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {required_amount} points to match the counter-bond, but you have {poster_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        poster_profile.rewards -= required_amount
        poster_profile.save()

        dispute.poster_deposit_amount = required_amount
        dispute.counter_bond_status = 'posted'
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-required_amount,
            transaction_type='dispute_deposit',
            description=f"Security counter-bond held for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=dispute.raised_by,
            message=f"{request.user.username} has posted the matching counter-bond for task '{task.title}'. Dispute is now in community jury review.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Matching counter-bond of {required_amount} points posted successfully. The dispute is now under community jury review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.check_counter_bond_timeout():
        messages.error(request, "This dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open' or dispute.counter_bond_status != 'posted':
        messages.error(request, "Voting is not active for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Constraint: Task poster and task worker cannot vote as jurors on their own dispute
    if request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by:
        messages.error(request, "Task posters and task workers cannot vote as jurors on their own disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a juror vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['worker', 'poster']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    stake_amount = 25
    juror_profile = request.user.userprofile

    # Constraint: Juror must hold enough reward points before casting a staked vote
    if juror_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {stake_amount} points to stake as a juror, but you have {juror_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_profile.rewards -= stake_amount
        juror_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote=vote_choice,
            stake_amount=stake_amount
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake held for vote on dispute for task: '{task.title}'"
        )

    messages.success(request, f"Juror vote recorded with {stake_amount} points staked.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Staff or task participants can trigger resolution once voting is complete
    if not (request.user.is_staff or request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by):
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.resolve_dispute_with_jury()
    messages.success(request, f"Dispute for task '{task.title}' has been resolved based on jury voting.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task

    with transaction.atomic():
        # Refund worker deposit
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )

        # Refund poster counter-bond if posted
        if dispute.counter_bond_status == 'posted' and dispute.poster_deposit_amount > 0 and task.posted_by:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += dispute.poster_deposit_amount
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=dispute.poster_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Security counter-bond refunded for withdrawn dispute on task: '{task.title}'"
            )

        # Refund any juror stakes
        for vote in dispute.votes.all():
            voter_profile = vote.voter.userprofile
            voter_profile.rewards += vote.stake_amount
            voter_profile.save()

            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=vote.stake_amount,
                transaction_type='juror_reward',
                description=f"Juror stake refunded for withdrawn dispute on task: '{task.title}'"
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
