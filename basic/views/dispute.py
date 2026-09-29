import math
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeVote, Task, Notification, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    user = request.user

    # Auto check counter bond SLA or voting SLA if expired
    if dispute.status == 'open' and dispute.counter_bond_deadline and timezone.now() > dispute.counter_bond_deadline:
        if not dispute.is_fully_backed:
            resolve_expired_counter_bond(dispute)
    elif dispute.status in ['open', 'voting'] and dispute.voting_deadline and timezone.now() > dispute.voting_deadline:
        resolve_dispute_voting(dispute)

    dispute.refresh_from_db()

    is_participant = (user == task.posted_by or user == task.taken_by or user == dispute.raised_by)
    
    # Counterparty check
    is_counterparty = False
    if dispute.status == 'open' and not dispute.is_fully_backed:
        if dispute.raised_by == task.taken_by and user == task.posted_by and dispute.poster_deposit_amount == 0:
            is_counterparty = True
        elif dispute.raised_by == task.posted_by and user == task.taken_by and dispute.worker_deposit_amount == 0:
            is_counterparty = True

    has_voted = DisputeVote.objects.filter(dispute=dispute, voter=user).exists()
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=user).first() if has_voted else None

    can_vote = (
        not is_participant and
        dispute.status in ['open', 'voting'] and
        dispute.is_fully_backed and
        not has_voted and
        (not dispute.voting_deadline or timezone.now() <= dispute.voting_deadline)
    )

    poster_votes_count = dispute.votes.filter(vote='poster').count()
    worker_votes_count = dispute.votes.filter(Q(vote='worker') | Q(vote='taker')).count()
    total_votes_count = dispute.votes.count()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_counterparty': is_counterparty,
        'can_vote': can_vote,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes_count': poster_votes_count,
        'worker_votes_count': worker_votes_count,
        'total_votes_count': total_votes_count,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'voting']:
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user not in [task.taken_by, task.posted_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are participating in that is currently in progress.")
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

        now = timezone.now()
        counter_deadline = now + timedelta(hours=48)

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
                dispute.counter_bond_deadline = counter_deadline
                dispute.voting_deadline = None
                if request.user == task.taken_by:
                    dispute.worker_deposit_amount = deposit_amount
                    dispute.worker_escrow_status = 'held'
                    dispute.poster_deposit_amount = 0
                    dispute.poster_escrow_status = 'held'
                    counterparty = task.posted_by
                else:
                    dispute.poster_deposit_amount = deposit_amount
                    dispute.poster_escrow_status = 'held'
                    dispute.worker_deposit_amount = 0
                    dispute.worker_escrow_status = 'held'
                    counterparty = task.taken_by
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='open',
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    worker_deposit_amount=deposit_amount if request.user == task.taken_by else 0,
                    worker_escrow_status='held',
                    poster_deposit_amount=deposit_amount if request.user == task.posted_by else 0,
                    poster_escrow_status='held',
                    counter_bond_deadline=counter_deadline
                )
                counterparty = task.posted_by if request.user == task.taken_by else task.taken_by

            RewardLedger.objects.create(
                user=request.user,
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
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'. Please deposit a matching bond of {deposit_amount} points within 48 hours.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Counterparty has 48 hours to match.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def pay_counter_bond(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for counter-staking.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Determine counterparty
    if dispute.raised_by == task.taken_by:
        if request.user != task.posted_by:
            messages.error(request, "Only the task poster can deposit the counter-bond for this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        if dispute.poster_deposit_amount > 0 and dispute.poster_escrow_status == 'held':
            messages.info(request, "Counter-bond has already been deposited.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        is_poster_paying = True
    elif dispute.raised_by == task.posted_by:
        if request.user != task.taken_by:
            messages.error(request, "Only the task worker can deposit the counter-bond for this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        if dispute.worker_deposit_amount > 0 and dispute.worker_escrow_status == 'held':
            messages.info(request, "Counter-bond has already been deposited.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        is_poster_paying = False
    else:
        messages.error(request, "Invalid dispute raiser.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.counter_bond_deadline and timezone.now() > dispute.counter_bond_deadline:
        resolve_expired_counter_bond(dispute)
        messages.error(request, "The 48-hour window to match the dispute bond has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {deposit_amount} points to match the counter-bond, but you have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    now = timezone.now()
    voting_deadline = now + timedelta(hours=72)

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        if is_poster_paying:
            dispute.poster_deposit_amount = deposit_amount
            dispute.poster_escrow_status = 'held'
        else:
            dispute.worker_deposit_amount = deposit_amount
            dispute.worker_escrow_status = 'held'

        dispute.status = 'voting'
        dispute.voting_deadline = voting_deadline
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type='poster_counter_bond' if is_poster_paying else 'dispute_deposit',
            description=f"Counter deposit bond held for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=dispute.raised_by,
            message=f"{request.user.username} has matched the dispute deposit bond for '{task.title}'. The dispute is now open for community juror voting.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Counter-bond of {deposit_amount} points deposited successfully! Juror voting is now open for 72 hours.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    user = request.user

    if dispute.status not in ['open', 'voting'] or not dispute.is_fully_backed:
        messages.error(request, "Unbacked disputes cannot enter juror voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if user == task.posted_by or user == task.taken_by or user == dispute.raised_by:
        messages.error(request, "You cannot vote as a juror on your own task or dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.voting_deadline and timezone.now() > dispute.voting_deadline:
        resolve_dispute_voting(dispute)
        messages.error(request, "The voting period for this dispute has closed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'worker', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if vote_choice == 'taker':
        vote_choice = 'worker'

    stake_amount = 20
    user_profile = user.userprofile
    if user_profile.rewards < stake_amount:
        messages.error(request, f"Insufficient reward points balance. You need at least {stake_amount} points to stake for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= stake_amount
        user_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=user,
            vote=vote_choice,
            stake_amount=stake_amount
        )

        RewardLedger.objects.create(
            user=user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake locked for voting on dispute: '{task.title}'"
        )

    messages.success(request, f"Your vote has been submitted! {stake_amount} points staked.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task

    if dispute.status == 'resolved':
        messages.error(request, "This dispute is already resolved.")
        return redirect('my_tasks')

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )

        for v in dispute.votes.all():
            voter_profile = v.voter.userprofile
            voter_profile.rewards += v.stake_amount
            voter_profile.save()
            RewardLedger.objects.create(
                user=v.voter,
                task=task,
                amount=v.stake_amount,
                transaction_type='juror_stake_refund',
                description=f"Juror stake refunded for withdrawn dispute on task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

def resolve_expired_counter_bond(dispute):
    task = dispute.task
    if dispute.status != 'open' or dispute.is_fully_backed:
        return

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.save()

        if dispute.raised_by == task.taken_by:
            # Worker raised dispute, poster failed to match counter-bond -> Worker wins automatically
            dispute.refund_worker_deposit(
                reason_description=f"Deposit bond refunded for auto-resolved dispute (poster failed to counter-stake) on task: '{task.title}'"
            )
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded task reward for auto-resolved dispute (poster failed to counter-stake) on task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Poster failed to deposit counter-bond within 48 hours for '{task.title}'. You won the dispute automatically!",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"You failed to deposit counter-bond within 48 hours for dispute on '{task.title}'. The dispute was resolved in favor of the worker.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        else:
            # Poster raised dispute, worker failed to match counter-bond -> Poster wins automatically
            dispute.refund_poster_deposit(
                reason_description=f"Deposit bond refunded for auto-resolved dispute (worker failed to counter-stake) on task: '{task.title}'"
            )
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for auto-resolved dispute (worker failed to counter-stake) on task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"You failed to deposit counter-bond within 48 hours for dispute on '{task.title}'. The dispute was resolved in favor of the poster.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Worker failed to deposit counter-bond within 48 hours for '{task.title}'. You won the dispute automatically!",
                link=reverse('dispute_detail', args=[dispute.id])
            )

def resolve_dispute_voting(dispute):
    task = dispute.task
    if dispute.status == 'resolved':
        return

    with transaction.atomic():
        poster_votes = dispute.votes.filter(vote='poster').count()
        worker_votes = dispute.votes.filter(Q(vote='worker') | Q(vote='taker')).count()
        total_votes = poster_votes + worker_votes

        if total_votes < 3:
            # Quorum not met -> refund both deposit bonds and all juror stakes
            dispute.refund_deposit(
                reason_description=f"Deposit bonds refunded due to insufficient voting quorum on task: '{task.title}'"
            )
            for v in dispute.votes.all():
                voter_profile = v.voter.userprofile
                voter_profile.rewards += v.stake_amount
                voter_profile.save()
                RewardLedger.objects.create(
                    user=v.voter,
                    task=task,
                    amount=v.stake_amount,
                    transaction_type='juror_stake_refund',
                    description=f"Juror stake refunded due to insufficient voting quorum on task: '{task.title}'"
                )
            dispute.status = 'resolved'
            dispute.save()
            task.status = 'in_progress'
            task.save()
            return

        if poster_votes == worker_votes:
            # Tie -> refund both deposit bonds and all juror stakes
            dispute.refund_deposit(
                reason_description=f"Deposit bonds refunded due to tie vote on task: '{task.title}'"
            )
            for v in dispute.votes.all():
                voter_profile = v.voter.userprofile
                voter_profile.rewards += v.stake_amount
                voter_profile.save()
                RewardLedger.objects.create(
                    user=v.voter,
                    task=task,
                    amount=v.stake_amount,
                    transaction_type='juror_stake_refund',
                    description=f"Juror stake refunded due to tie vote on task: '{task.title}'"
                )
            dispute.status = 'resolved'
            dispute.save()
            task.status = 'in_progress'
            task.save()
            return

        if worker_votes > poster_votes:
            winner = task.taken_by
            loser = task.posted_by
            winning_vote = 'worker'
            if winner:
                winner_profile = winner.userprofile
                winner_profile.rewards += task.reward
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded task reward for dispute victory on task: '{task.title}'"
                )
            task.status = 'completed'
        else:
            winner = task.posted_by
            loser = task.taken_by
            winning_vote = 'poster'
            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refunded task reward for dispute victory on task: '{task.title}'"
            )
            task.status = 'cancelled'

        task.save()

        # Escrow Bond Settlement:
        # Refund winning party deposit bond & Forfeit losing party deposit bond to winner
        if winning_vote == 'worker':
            dispute.refund_worker_deposit(
                reason_description=f"Security deposit bond refunded for dispute victory on task: '{task.title}'"
            )
            dispute.forfeit_poster_deposit(
                beneficiary=winner,
                reason_description=f"Losing party security deposit bond forfeited for dispute on task: '{task.title}'"
            )
        else:
            dispute.refund_poster_deposit(
                reason_description=f"Security deposit bond refunded for dispute victory on task: '{task.title}'"
            )
            dispute.forfeit_worker_deposit(
                beneficiary=winner,
                reason_description=f"Losing party security deposit bond forfeited for dispute on task: '{task.title}'"
            )

        # Juror Settlement:
        winning_votes = dispute.votes.filter(Q(vote=winning_vote) | (Q(vote='taker') if winning_vote == 'worker' else Q()))
        losing_votes = dispute.votes.exclude(id__in=winning_votes.values_list('id', flat=True))

        slashed_stakes = sum(v.stake_amount for v in losing_votes)
        num_winning_jurors = winning_votes.count()
        pro_rata_reward = math.floor(slashed_stakes / num_winning_jurors) if num_winning_jurors > 0 else 0

        # Refund stake and award pro-rata reward to winning jurors
        for v in winning_votes:
            voter_profile = v.voter.userprofile
            voter_profile.rewards += v.stake_amount + pro_rata_reward
            voter_profile.save()

            RewardLedger.objects.create(
                user=v.voter,
                task=task,
                amount=v.stake_amount,
                transaction_type='juror_stake_refund',
                description=f"Juror stake refunded for majority vote on dispute: '{task.title}'"
            )
            if pro_rata_reward > 0:
                RewardLedger.objects.create(
                    user=v.voter,
                    task=task,
                    amount=pro_rata_reward,
                    transaction_type='juror_reward',
                    description=f"Pro-rata juror reward payout for majority vote on dispute: '{task.title}'"
                )

        # Slashed losing jurors
        for v in losing_votes:
            RewardLedger.objects.create(
                user=v.voter,
                task=task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Juror stake slashed for minority vote on dispute: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()
