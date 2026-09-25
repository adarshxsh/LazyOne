import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JurorVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    has_voted = JurorVote.objects.filter(dispute=dispute, juror=request.user).exists()
    user_vote = JurorVote.objects.filter(dispute=dispute, juror=request.user).first()

    poster_votes = dispute.votes.filter(vote='poster').count()
    worker_votes = dispute.votes.filter(vote='worker').count()
    total_votes = dispute.votes.count()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'total_votes': total_votes,
        'can_vote': not is_participant and dispute.status == 'open' and not has_voted,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user not in [task.taken_by, task.posted_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task in progress that you participated in.")
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        poster_profile = task.posted_by.userprofile
        worker_profile = task.taken_by.userprofile if task.taken_by else None

        if poster_profile.rewards < deposit_amount or (worker_profile and worker_profile.rewards < deposit_amount):
            if request.user.userprofile.rewards < deposit_amount:
                messages.error(
                    request,
                    f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {request.user.userprofile.rewards} points."
                )
            else:
                messages.error(
                    request,
                    f"Task counterparty does not have sufficient reward points ({deposit_amount} required) for symmetrical deposit bond."
                )
            return redirect('my_tasks')

        with transaction.atomic():
            poster_profile.rewards -= deposit_amount
            poster_profile.save()

            if worker_profile:
                worker_profile.rewards -= deposit_amount
                worker_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.poster_deposit_amount = deposit_amount
                dispute.worker_deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.poster_escrow_status = 'held'
                dispute.worker_escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    poster_deposit_amount=deposit_amount,
                    worker_deposit_amount=deposit_amount,
                    escrow_status='held',
                    poster_escrow_status='held',
                    worker_escrow_status='held'
                )

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=-deposit_amount,
                transaction_type='poster_dispute_deposit',
                description=f"Poster security deposit bond held for dispute on task: '{task.title}'"
            )

            if task.taken_by:
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=-deposit_amount,
                    transaction_type='worker_dispute_deposit',
                    description=f"Worker security deposit bond held for dispute on task: '{task.title}'"
                )

            task.status = 'disputed'
            task.save()

            recipient = task.posted_by if request.user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as symmetrical deposit bond.")
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

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Deposit bonds have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user in [task.posted_by, task.taken_by]:
        messages.error(request, "Task participants cannot vote as jurors.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'worker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    JUROR_STAKE_AMOUNT = 50
    juror_profile = request.user.userprofile
    if juror_profile.rewards < JUROR_STAKE_AMOUNT:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {JUROR_STAKE_AMOUNT} points to vote as a juror."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_profile.rewards -= JUROR_STAKE_AMOUNT
        juror_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-JUROR_STAKE_AMOUNT,
            transaction_type='juror_stake_lock',
            description=f"Juror stake locked for dispute on task: '{task.title}'"
        )

        JurorVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote=vote_choice,
            staked_amount=JUROR_STAKE_AMOUNT
        )

    messages.success(request, f"Vote submitted successfully. {JUROR_STAKE_AMOUNT} points locked as juror stake.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "Dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner = request.POST.get('winner')
    if winner not in ['poster', 'worker']:
        poster_votes = dispute.votes.filter(vote='poster').count()
        worker_votes = dispute.votes.filter(vote='worker').count()
        if poster_votes > worker_votes:
            winner = 'poster'
        elif worker_votes > poster_votes:
            winner = 'worker'
        else:
            winner = 'poster' if dispute.raised_by == task.posted_by else 'worker'

    with transaction.atomic():
        settle_dispute_internal(dispute, winner)

    messages.success(request, f"Dispute resolved in favor of the {winner}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def settle_dispute_internal(dispute, winner):
    task = dispute.task
    poster = task.posted_by
    worker = task.taken_by

    poster_profile = poster.userprofile
    worker_profile = worker.userprofile if worker else None

    if winner == 'poster':
        if dispute.poster_escrow_status == 'held' and dispute.poster_deposit_amount > 0:
            poster_profile.rewards += dispute.poster_deposit_amount
            poster_profile.save()
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=dispute.poster_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Security deposit bond refunded to winning poster for task: '{task.title}'"
            )
            dispute.poster_escrow_status = 'refunded'

        forfeited_bond = dispute.worker_deposit_amount
        if dispute.worker_escrow_status == 'held' and forfeited_bond > 0:
            RewardLedger.objects.create(
                user=worker,
                task=task,
                amount=0,
                transaction_type='dispute_forfeit',
                description=f"Security deposit bond forfeited by worker for task: '{task.title}'"
            )
            dispute.worker_escrow_status = 'forfeited'

        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=poster,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Refund for cancelled task upon dispute resolution: '{task.title}'"
        )
        task.status = 'cancelled'
        task.save()

    else:  # winner == 'worker'
        if worker and dispute.worker_escrow_status == 'held' and dispute.worker_deposit_amount > 0:
            worker_profile.rewards += dispute.worker_deposit_amount
            worker_profile.save()
            RewardLedger.objects.create(
                user=worker,
                task=task,
                amount=dispute.worker_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Security deposit bond refunded to winning worker for task: '{task.title}'"
            )
            dispute.worker_escrow_status = 'refunded'

        forfeited_bond = dispute.poster_deposit_amount
        if dispute.poster_escrow_status == 'held' and forfeited_bond > 0:
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=0,
                transaction_type='dispute_forfeit',
                description=f"Security deposit bond forfeited by poster for task: '{task.title}'"
            )
            dispute.poster_escrow_status = 'forfeited'

        if worker:
            worker_profile.rewards += task.reward
            worker_profile.save()
            RewardLedger.objects.create(
                user=worker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Awarded task reward upon dispute resolution: '{task.title}'"
            )
        task.status = 'completed'
        task.save()

    all_votes = dispute.votes.all()
    majority_votes = [v for v in all_votes if v.vote == winner]
    minority_votes = [v for v in all_votes if v.vote != winner]

    slashed_stakes_total = 0
    for mv in minority_votes:
        slashed_stakes_total += mv.staked_amount
        RewardLedger.objects.create(
            user=mv.juror,
            task=task,
            amount=0,
            transaction_type='juror_vote_slash',
            description=f"Juror stake slashed for minority vote on dispute: '{task.title}'"
        )

    reward_pool = forfeited_bond + slashed_stakes_total
    num_majority = len(majority_votes)

    if num_majority > 0:
        share_per_juror = math.floor(reward_pool / num_majority)
        residual = reward_pool - (share_per_juror * num_majority)

        for maj_vote in majority_votes:
            j_profile = maj_vote.juror.userprofile
            payout = maj_vote.staked_amount + share_per_juror
            j_profile.rewards += payout
            j_profile.save()

            RewardLedger.objects.create(
                user=maj_vote.juror,
                task=task,
                amount=payout,
                transaction_type='juror_reward',
                description=f"Juror stake refunded ({maj_vote.staked_amount}) plus pool reward share ({share_per_juror}) for dispute: '{task.title}'"
            )

        if residual > 0:
            winning_user = poster if winner == 'poster' else worker
            if winning_user:
                w_profile = winning_user.userprofile
                w_profile.rewards += residual
                w_profile.save()
                RewardLedger.objects.create(
                    user=winning_user,
                    task=task,
                    amount=residual,
                    transaction_type='dispute_refund',
                    description=f"Dispute pool residual reward awarded to winning party for task: '{task.title}'"
                )
    else:
        winning_user = poster if winner == 'poster' else worker
        if winning_user and reward_pool > 0:
            w_profile = winning_user.userprofile
            w_profile.rewards += reward_pool
            w_profile.save()
            RewardLedger.objects.create(
                user=winning_user,
                task=task,
                amount=reward_pool,
                transaction_type='dispute_refund',
                description=f"Dispute pool forfeited funds awarded to winning party for task: '{task.title}'"
            )

    dispute.status = 'resolved'
    dispute.escrow_status = 'refunded'
    dispute.save()
