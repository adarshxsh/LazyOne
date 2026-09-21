import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
    is_participant = request.user in [task.posted_by, task.taken_by]
    poster_votes = dispute.votes.filter(voted_option='poster').count()
    taker_votes = dispute.votes.filter(voted_option='taker').count()
    total_votes = dispute.votes.count()

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
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

        # Check poster balance or ensure poster deposit status is held
        poster_profile = task.posted_by.userprofile

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
                dispute.poster_deposit_amount = task.deposit_bond_amount
                dispute.poster_deposit_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    poster_deposit_amount=task.deposit_bond_amount,
                    poster_deposit_status='held'
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
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and taker cannot vote as community jurors on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status not in ['open', 'voting']:
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_option = request.POST.get('voted_option')
    if voted_option not in ['poster', 'taker']:
        messages.error(request, "Invalid vote option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        stake_amount = int(request.POST.get('staked_amount', 50))
    except (ValueError, TypeError):
        stake_amount = 50

    if stake_amount < 50:
        stake_amount = 50

    juror_profile = request.user.userprofile
    if juror_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward points. You need at least {stake_amount} points to cast a staked vote."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_profile.rewards -= stake_amount
        juror_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_option=voted_option,
            staked_amount=stake_amount
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake_held',
            description=f"Juror stake held for vote on dispute for task: '{task.title}'"
        )

        dispute.total_juror_stake += stake_amount
        if dispute.status == 'open':
            dispute.status = 'voting'
        dispute.save()

    messages.success(request, f"Vote submitted successfully. {stake_amount} points locked as juror stake commitment.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def process_dispute_resolution(dispute, winner_override=None):
    with transaction.atomic():
        if dispute.status == 'resolved':
            return

        task = dispute.task
        taker = task.taken_by
        poster = task.posted_by

        if winner_override in ['poster', 'taker']:
            winner = winner_override
        else:
            poster_votes = dispute.votes.filter(voted_option='poster').count()
            taker_votes = dispute.votes.filter(voted_option='taker').count()
            if taker_votes > poster_votes:
                winner = 'taker'
            else:
                winner = 'poster'

        losing_party_bond = 0

        if winner == 'taker':
            # Award task reward to taker
            if taker:
                taker_profile = taker.userprofile
                taker_profile.rewards += task.reward
                RewardLedger.objects.create(
                    user=taker,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Task reward awarded for winning dispute on task: '{task.title}'"
                )

                # Refund taker deposit bond
                if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                    taker_profile.rewards += dispute.deposit_amount
                    RewardLedger.objects.create(
                        user=taker,
                        task=task,
                        amount=dispute.deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                    )
                    dispute.escrow_status = 'refunded'
                taker_profile.save()

            # Forfeit poster deposit bond
            if dispute.poster_deposit_status == 'held' and dispute.poster_deposit_amount > 0:
                losing_party_bond = dispute.poster_deposit_amount
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Poster deposit bond forfeited for losing dispute on task: '{task.title}'"
                )
                dispute.poster_deposit_status = 'forfeited'

            task.status = 'completed'
            task.save()

        else: # winner == 'poster'
            # Refund task reward to poster
            poster_profile = poster.userprofile
            poster_profile.rewards += task.reward
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for winning dispute on task: '{task.title}'"
            )

            # Refund poster deposit bond
            if dispute.poster_deposit_status == 'held' and dispute.poster_deposit_amount > 0:
                poster_profile.rewards += dispute.poster_deposit_amount
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=dispute.poster_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Poster deposit bond refunded for winning dispute on task: '{task.title}'"
                )
                dispute.poster_deposit_status = 'refunded'
            poster_profile.save()

            # Forfeit taker deposit bond
            if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                losing_party_bond = dispute.deposit_amount
                if taker:
                    RewardLedger.objects.create(
                        user=taker,
                        task=task,
                        amount=0,
                        transaction_type='dispute_forfeit',
                        description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                    )
                dispute.escrow_status = 'forfeited'

            task.status = 'cancelled'
            task.save()

        # Jurors handling
        all_votes = dispute.votes.all()
        majority_votes = all_votes.filter(voted_option=winner)
        minority_votes = all_votes.exclude(voted_option=winner)

        # Minority vote slashing
        slashed_stakes_total = 0
        for min_vote in minority_votes:
            slashed_stakes_total += min_vote.staked_amount
            RewardLedger.objects.create(
                user=min_vote.voter,
                task=task,
                amount=0,
                transaction_type='juror_stake_slashed',
                description=f"Juror stake slashed for minority vote on dispute for task: '{task.title}'"
            )

        # Dividend Pool = Slashed minority stakes + Losing party's deposit bond
        dividend_pool = slashed_stakes_total + losing_party_bond
        total_majority_staked = sum(v.staked_amount for v in majority_votes)

        if majority_votes.exists() and total_majority_staked > 0:
            for maj_vote in majority_votes:
                maj_profile = maj_vote.voter.userprofile
                # 1. Full stake refund
                maj_profile.rewards += maj_vote.staked_amount
                RewardLedger.objects.create(
                    user=maj_vote.voter,
                    task=task,
                    amount=maj_vote.staked_amount,
                    transaction_type='juror_stake_refunded',
                    description=f"Juror stake refunded for majority vote on dispute for task: '{task.title}'"
                )

                # 2. Pro-rata dividend payout
                dividend_share = math.floor(dividend_pool * (maj_vote.staked_amount / total_majority_staked))
                if dividend_share > 0:
                    maj_profile.rewards += dividend_share
                    RewardLedger.objects.create(
                        user=maj_vote.voter,
                        task=task,
                        amount=dividend_share,
                        transaction_type='juror_reward_payout',
                        description=f"Juror reward dividend payout for dispute on task: '{task.title}'"
                    )
                maj_profile.save()

        dispute.status = 'resolved'
        dispute.save()

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if not request.user.is_staff and request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_choice = request.POST.get('winner') if request.user.is_staff else None
    process_dispute_resolution(dispute, winner_override=winner_choice)

    messages.success(request, f"Dispute for '{task.title}' resolved successfully.")
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
        dispute.status = 'withdrawn'
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
