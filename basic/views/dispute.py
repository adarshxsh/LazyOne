from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, JurorVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    user_vote = None
    if request.user.is_authenticated:
        user_vote = dispute.juror_votes.filter(juror=request.user).first()

    can_vote = (
        request.user.is_authenticated
        and request.user != task.posted_by
        and request.user != task.taken_by
        and dispute.status == 'voting'
        and user_vote is None
    )

    can_counter_bond = (
        request.user.is_authenticated
        and request.user == task.posted_by
        and dispute.status == 'open'
        and dispute.poster_escrow_status == 'pending'
    )

    show_vote_counts = (
        dispute.status == 'resolved'
        or user_vote is not None
        or request.user == task.posted_by
        or request.user == task.taken_by
        or request.user.is_staff
    )

    worker_votes_count = dispute.juror_votes.filter(vote='worker').count()
    poster_votes_count = dispute.juror_votes.filter(vote='poster').count()
    total_votes_count = dispute.juror_votes.count()

    minority_stakes = 0
    if dispute.status == 'resolved':
        minority_stakes = sum(v.stake_amount for v in dispute.juror_votes.filter(is_slashed=True))
    
    juror_reward_pool = dispute.poster_deposit_amount + dispute.worker_deposit_amount + minority_stakes

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'can_counter_bond': can_counter_bond,
        'show_vote_counts': show_vote_counts,
        'worker_votes_count': worker_votes_count,
        'poster_votes_count': poster_votes_count,
        'total_votes_count': total_votes_count,
        'juror_reward_pool': juror_reward_pool,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'voting']:
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

        counter_bond_deadline = timezone.now() + timedelta(hours=48)

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.worker_deposit_amount = deposit_amount
                dispute.worker_escrow_status = 'held'
                dispute.poster_deposit_amount = 0
                dispute.poster_escrow_status = 'pending'
                dispute.counter_bond_deadline = counter_bond_deadline
                dispute.voting_deadline = None
                dispute.consensus_outcome = 'pending'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    worker_deposit_amount=deposit_amount,
                    worker_escrow_status='held',
                    poster_deposit_amount=0,
                    poster_escrow_status='pending',
                    status='open',
                    counter_bond_deadline=counter_bond_deadline,
                    consensus_outcome='pending'
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
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. Matching counter-bond required within 48h.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Poster has 48h to submit matching counter-bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def post_dispute_counter_bond(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by:
        messages.error(request, "Only the task poster can submit the matching counter-bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open' or dispute.poster_escrow_status != 'pending':
        messages.error(request, "This dispute is not currently awaiting a counter-bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    poster_amount = task.deposit_bond_amount
    poster_profile = request.user.userprofile

    if poster_profile.rewards < poster_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {poster_amount} points as a matching counter-bond, but you only have {poster_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    voting_deadline = timezone.now() + timedelta(hours=48)

    with transaction.atomic():
        poster_profile.rewards -= poster_amount
        poster_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-poster_amount,
            transaction_type='dispute_poster_deposit',
            description=f"Matching deposit bond held for dispute on task: '{task.title}'"
        )

        dispute.poster_deposit_amount = poster_amount
        dispute.poster_escrow_status = 'held'
        dispute.status = 'voting'
        dispute.voting_deadline = voting_deadline
        dispute.save()

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Poster {request.user.username} submitted matching counter-bond for '{task.title}'. Juror voting is now open.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Matching deposit bond of {poster_amount} points posted successfully. Community juror voting is now active.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'voting':
        messages.error(request, "Juror voting is not open for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and task worker cannot participate as jurors.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast a vote in this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['worker', 'poster']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    stake_amount = task.deposit_bond_amount
    juror_profile = request.user.userprofile

    if juror_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient points balance. You need at least {stake_amount} points to lock as stake to vote."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_profile.rewards -= stake_amount
        juror_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake_lock',
            description=f"Juror stake locked for dispute vote on task: '{task.title}'"
        )

        JurorVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote=vote_choice,
            stake_amount=stake_amount
        )

    messages.success(request, f"Your vote for '{vote_choice}' has been cast and {stake_amount} points have been locked as stake.")
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
        if dispute.poster_escrow_status == 'held' and dispute.poster_deposit_amount > 0:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += dispute.poster_deposit_amount
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=dispute.poster_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Matching deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )
            dispute.poster_escrow_status = 'refunded'

        if dispute.status == 'voting':
            for vote in dispute.juror_votes.all():
                juror_profile = vote.juror.userprofile
                juror_profile.rewards += vote.stake_amount
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.juror,
                    task=task,
                    amount=vote.stake_amount,
                    transaction_type='juror_reward_payout',
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
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

def settle_dispute_voting_outcome(dispute):
    task = dispute.task
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update().get(id=dispute.id)
        if dispute.status == 'resolved':
            return

        votes = list(dispute.juror_votes.all())
        worker_votes = [v for v in votes if v.vote == 'worker']
        poster_votes = [v for v in votes if v.vote == 'poster']

        worker_count = len(worker_votes)
        poster_count = len(poster_votes)

        if worker_count > poster_count:
            outcome = 'worker_wins'
        elif poster_count > worker_count:
            outcome = 'poster_wins'
        else:
            outcome = 'tie'

        dispute.consensus_outcome = outcome

        if outcome == 'worker_wins':
            # Worker (Winner): Task Reward + deposit bond refund
            if task.taken_by:
                worker_profile = task.taken_by.userprofile
                worker_profile.rewards += task.reward + dispute.worker_deposit_amount
                worker_profile.save()

                dispute.worker_escrow_status = 'refunded'
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward for winning dispute on task: '{task.title}'"
                )
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=dispute.worker_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Deposit bond refunded for winning dispute on task: '{task.title}'"
                )

            # Poster (Loser): forfeits deposit bond
            dispute.poster_escrow_status = 'forfeited'
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=0,
                transaction_type='dispute_forfeit',
                description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'"
            )

            task.status = 'completed'
            task.save()

            # Juror pool distribution
            minority_slashed_sum = sum(v.stake_amount for v in poster_votes)
            for v in poster_votes:
                v.is_slashed = True
                v.reward_amount = 0
                v.save()
                RewardLedger.objects.create(
                    user=v.juror, task=task, amount=0,
                    transaction_type='juror_stake_slash',
                    description=f"Juror stake slashed for minority vote on task: '{task.title}'"
                )

            juror_reward_pool = dispute.poster_deposit_amount + minority_slashed_sum
            total_majority_stake = sum(v.stake_amount for v in worker_votes)

            if total_majority_stake > 0:
                remainder = juror_reward_pool
                for i, v in enumerate(worker_votes):
                    if i == len(worker_votes) - 1:
                        share = remainder
                    else:
                        share = (juror_reward_pool * v.stake_amount) // total_majority_stake
                        remainder -= share
                    payout = v.stake_amount + share
                    v.reward_amount = share
                    v.is_slashed = False
                    v.save()

                    juror_profile = v.juror.userprofile
                    juror_profile.rewards += payout
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=v.juror, task=task, amount=payout,
                        transaction_type='juror_reward_payout',
                        description=f"Juror stake refund and reward payout for majority vote on task: '{task.title}'"
                    )

        elif outcome == 'poster_wins':
            # Poster (Winner): Task Reward + deposit bond refund
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward + dispute.poster_deposit_amount
            poster_profile.save()

            dispute.poster_escrow_status = 'refunded'
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for winning dispute on task: '{task.title}'"
            )
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=dispute.poster_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Deposit bond refunded for winning dispute on task: '{task.title}'"
            )

            # Worker (Loser): forfeits deposit bond
            if task.taken_by:
                dispute.worker_escrow_status = 'forfeited'
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'"
                )

            task.status = 'cancelled'
            task.save()

            # Juror pool distribution
            minority_slashed_sum = sum(v.stake_amount for v in worker_votes)
            for v in worker_votes:
                v.is_slashed = True
                v.reward_amount = 0
                v.save()
                RewardLedger.objects.create(
                    user=v.juror, task=task, amount=0,
                    transaction_type='juror_stake_slash',
                    description=f"Juror stake slashed for minority vote on task: '{task.title}'"
                )

            juror_reward_pool = dispute.worker_deposit_amount + minority_slashed_sum
            total_majority_stake = sum(v.stake_amount for v in poster_votes)

            if total_majority_stake > 0:
                remainder = juror_reward_pool
                for i, v in enumerate(poster_votes):
                    if i == len(poster_votes) - 1:
                        share = remainder
                    else:
                        share = (juror_reward_pool * v.stake_amount) // total_majority_stake
                        remainder -= share
                    payout = v.stake_amount + share
                    v.reward_amount = share
                    v.is_slashed = False
                    v.save()

                    juror_profile = v.juror.userprofile
                    juror_profile.rewards += payout
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=v.juror, task=task, amount=payout,
                        transaction_type='juror_reward_payout',
                        description=f"Juror stake refund and reward payout for majority vote on task: '{task.title}'"
                    )

        else:  # 'tie' or zero votes
            if dispute.worker_escrow_status == 'held' and dispute.worker_deposit_amount > 0 and task.taken_by:
                worker_profile = task.taken_by.userprofile
                worker_profile.rewards += dispute.worker_deposit_amount
                worker_profile.save()
                dispute.worker_escrow_status = 'refunded'
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=dispute.worker_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Deposit bond refunded on tie dispute for task: '{task.title}'"
                )

            if dispute.poster_escrow_status == 'held' and dispute.poster_deposit_amount > 0:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += dispute.poster_deposit_amount
                poster_profile.save()
                dispute.poster_escrow_status = 'refunded'
                RewardLedger.objects.create(
                    user=task.posted_by, task=task, amount=dispute.poster_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Deposit bond refunded on tie dispute for task: '{task.title}'"
                )

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by, task=task, amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded on tie dispute for task: '{task.title}'"
            )

            task.status = 'cancelled'
            task.save()

            for v in votes:
                v.is_slashed = False
                v.reward_amount = 0
                v.save()
                juror_profile = v.juror.userprofile
                juror_profile.rewards += v.stake_amount
                juror_profile.save()
                RewardLedger.objects.create(
                    user=v.juror, task=task, amount=v.stake_amount,
                    transaction_type='juror_reward_payout',
                    description=f"Juror stake refunded on tie dispute for task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.save()
