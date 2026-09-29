import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

JUROR_MICRO_STAKE = 10

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()
    total_votes = dispute.votes.count()

    user_vote = dispute.votes.filter(voter=request.user).first()
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote = (dispute.status == 'open' and not is_participant and user_vote is None)

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
        'micro_stake': JUROR_MICRO_STAKE,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    
    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are a party to that is currently in progress.")
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        raiser_profile = request.user.userprofile
        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by

        if raiser_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {raiser_profile.rewards} points."
            )
            return redirect('my_tasks')

        if counterparty:
            counterparty_profile = counterparty.userprofile
            if counterparty_profile.rewards < deposit_amount:
                messages.error(
                    request,
                    f"Insufficient counterparty reward points balance. Dispute requires matching deposit bonds of {deposit_amount} points from both parties."
                )
                return redirect('my_tasks')

        with transaction.atomic():
            raiser_profile.rewards -= deposit_amount
            raiser_profile.save()

            if counterparty:
                counterparty_profile.rewards -= deposit_amount
                counterparty_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.poster_deposit_amount = deposit_amount
                dispute.poster_escrow_status = 'held'
                dispute.taker_deposit_amount = deposit_amount
                dispute.taker_escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    poster_deposit_amount=deposit_amount,
                    poster_escrow_status='held',
                    taker_deposit_amount=deposit_amount,
                    taker_escrow_status='held',
                )

            # Record ledger for raiser
            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            # Record ledger for counterparty
            if counterparty:
                RewardLedger.objects.create(
                    user=counterparty,
                    task=task,
                    amount=-deposit_amount,
                    transaction_type='dispute_deposit',
                    description=f"Security deposit bond held for dispute on task: '{task.title}'"
                )

            task.status = 'disputed'
            task.save()

            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'. Matching deposit bond has been locked.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. Matching deposit bonds of {deposit_amount} points held for poster and taker.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. Deposit bonds refunded and task returned to in-progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Deposit bonds have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and takers cannot vote as jurors on their own disputed tasks.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    micro_stake = JUROR_MICRO_STAKE
    user_profile = request.user.userprofile

    if user_profile.rewards < micro_stake:
        messages.error(
            request,
            f"Insufficient reward balance. You need at least {micro_stake} points to stake a micro-deposit and vote, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= micro_stake
        user_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote=vote_choice,
            staked_amount=micro_stake
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-micro_stake,
            transaction_type='juror_stake',
            description=f"Juror micro-stake held for voting on dispute for task: '{task.title}'"
        )

    messages.success(request, f"Your vote for {vote_choice} has been submitted and {micro_stake} points micro-staked successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if not request.user.is_staff and request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    winner = request.POST.get('winner')
    if winner not in ['poster', 'taker']:
        messages.error(request, "Invalid winner choice for dispute resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        settle_dispute_atomic(dispute, winner)

    messages.success(request, f"Dispute resolved in favor of {winner}. Bond refunds, stake slashes, and rewards distributed.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def settle_dispute_atomic(dispute, winner):
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    bond_amount = task.deposit_bond_amount

    if winner == 'poster':
        poster_profile = poster.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=poster,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Task reward refunded upon dispute resolution for task: '{task.title}'"
        )
        task.status = 'cancelled'

        refund_bond = dispute.poster_deposit_amount or bond_amount
        poster_profile.rewards += refund_bond
        poster_profile.save()
        RewardLedger.objects.create(
            user=poster,
            task=task,
            amount=refund_bond,
            transaction_type='dispute_refund',
            description=f"Security deposit bond refunded upon dispute win for task: '{task.title}'"
        )
        dispute.poster_escrow_status = 'refunded'

        losing_forfeited_bond = dispute.taker_deposit_amount or bond_amount
        dispute.taker_escrow_status = 'forfeited'
        if taker:
            RewardLedger.objects.create(
                user=taker,
                task=task,
                amount=0,
                transaction_type='dispute_forfeit',
                description=f"Security deposit bond forfeited upon dispute loss for task: '{task.title}'"
            )

    else: # taker
        if taker:
            taker_profile = taker.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=taker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded upon dispute resolution for task: '{task.title}'"
            )

            refund_bond = dispute.taker_deposit_amount or bond_amount
            taker_profile.rewards += refund_bond
            taker_profile.save()
            RewardLedger.objects.create(
                user=taker,
                task=task,
                amount=refund_bond,
                transaction_type='dispute_refund',
                description=f"Security deposit bond refunded upon dispute win for task: '{task.title}'"
            )
            dispute.taker_escrow_status = 'refunded'

        task.status = 'completed'

        losing_forfeited_bond = dispute.poster_deposit_amount or bond_amount
        dispute.poster_escrow_status = 'forfeited'
        RewardLedger.objects.create(
            user=poster,
            task=task,
            amount=0,
            transaction_type='dispute_forfeit',
            description=f"Security deposit bond forfeited upon dispute loss for task: '{task.title}'"
        )

    task.save()
    dispute.escrow_status = 'forfeited'
    dispute.status = 'resolved'
    dispute.save()

    votes = list(dispute.votes.all())
    majority_votes = [v for v in votes if v.vote == winner]
    minority_votes = [v for v in votes if v.vote != winner]

    total_slashed_stakes = 0
    for min_vote in minority_votes:
        total_slashed_stakes += min_vote.staked_amount
        RewardLedger.objects.create(
            user=min_vote.voter,
            task=task,
            amount=-min_vote.staked_amount,
            transaction_type='juror_slash',
            description=f"Juror micro-stake slashed for minority vote on dispute for task: '{task.title}'"
        )

    for maj_vote in majority_votes:
        maj_profile = maj_vote.voter.userprofile
        maj_profile.rewards += maj_vote.staked_amount
        maj_profile.save()
        RewardLedger.objects.create(
            user=maj_vote.voter,
            task=task,
            amount=maj_vote.staked_amount,
            transaction_type='juror_stake_refund',
            description=f"Juror micro-stake refunded for majority vote on dispute for task: '{task.title}'"
        )

    total_reward_pool = losing_forfeited_bond + total_slashed_stakes
    if majority_votes and total_reward_pool > 0:
        per_maj_reward = math.floor(total_reward_pool / len(majority_votes))
        if per_maj_reward > 0:
            for maj_vote in majority_votes:
                maj_profile = maj_vote.voter.userprofile
                maj_profile.rewards += per_maj_reward
                maj_profile.save()
                RewardLedger.objects.create(
                    user=maj_vote.voter,
                    task=task,
                    amount=per_maj_reward,
                    transaction_type='juror_reward',
                    description=f"Juror reward payout share from dispute settlement on task: '{task.title}'"
                )
