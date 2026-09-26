import math
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    user_has_voted = False
    if request.user.is_authenticated:
        user_has_voted = dispute.votes.filter(voter=request.user).exists()
    
    can_vote = (
        request.user.is_authenticated
        and request.user != task.posted_by
        and request.user != task.taken_by
        and dispute.status == 'open'
        and dispute.poster_deposited
        and dispute.worker_deposited
        and not user_has_voted
        and hasattr(request.user, 'userprofile')
        and request.user.userprofile.rewards >= 50
    )

    needs_counter_bond = (
        dispute.status == 'open'
        and (
            (request.user == task.posted_by and not dispute.poster_deposited)
            or (request.user == task.taken_by and not dispute.worker_deposited)
        )
    )

    context = {
        'dispute': dispute,
        'task': task,
        'can_vote': can_vote,
        'user_has_voted': user_has_voted,
        'needs_counter_bond': needs_counter_bond,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    
    if request.user != task.taken_by and request.user != task.posted_by:
        messages.error(request, "You can only raise a dispute for a task you are involved in.")
        return redirect('my_tasks')
    
    if task.status != 'in_progress':
        messages.error(request, "Disputes can only be raised on tasks currently in progress.")
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

            now = timezone.now()
            deadline = now + timedelta(hours=24)

            is_worker = (request.user == task.taken_by)
            
            dispute, created = Dispute.objects.get_or_create(
                task=task,
                defaults={
                    'raised_by': request.user,
                    'reason': reason,
                    'status': 'open',
                    'deposit_amount': deposit_amount,
                    'worker_deposit_amount': deposit_amount if is_worker else 0,
                    'poster_deposit_amount': 0 if is_worker else deposit_amount,
                    'worker_deposited': is_worker,
                    'poster_deposited': not is_worker,
                    'counter_bond_deadline': deadline,
                    'escrow_status': 'held'
                }
            )
            if not created:
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.worker_deposit_amount = deposit_amount if is_worker else 0
                dispute.poster_deposit_amount = 0 if is_worker else deposit_amount
                dispute.worker_deposited = is_worker
                dispute.poster_deposited = not is_worker
                dispute.counter_bond_deadline = deadline
                dispute.escrow_status = 'held'
                dispute.save()

            transaction_type = 'dispute_deposit' if is_worker else 'poster_dispute_deposit'
            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type=transaction_type,
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            counterparty = task.posted_by if is_worker else task.taken_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'. Please deposit matching bond within 24 hours.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def deposit_counter_bond(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to deposit a counter-bond for this dispute.")
        return redirect('home')

    is_poster = (request.user == task.posted_by)
    if (is_poster and dispute.poster_deposited) or (not is_poster and dispute.worker_deposited):
        messages.info(request, "You have already deposited your deposit bond for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points. You need at least {deposit_amount} points to match the deposit bond, but you have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        if is_poster:
            dispute.poster_deposited = True
            dispute.poster_deposit_amount = deposit_amount
            transaction_type = 'poster_dispute_deposit'
        else:
            dispute.worker_deposited = True
            dispute.worker_deposit_amount = deposit_amount
            transaction_type = 'dispute_deposit'
        
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type=transaction_type,
            description=f"Matching deposit bond held for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=dispute.raised_by,
            message=f"{request.user.username} matched the deposit bond for task: '{task.title}'. The dispute is now open for jury review.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Matching deposit bond of {deposit_amount} points deposited successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user, status='open')
    task = dispute.task

    if dispute.votes.exists():
        messages.error(request, "Cannot withdraw dispute once jury voting has started.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
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

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Deposit bonds have been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and task taker cannot act as jurors on their own disputed task.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not (dispute.poster_deposited and dispute.worker_deposited):
        messages.error(request, "Jury voting is only allowed once both parties have deposited matching bonds.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    user_profile = request.user.userprofile
    if user_profile.rewards < 50:
        messages.error(request, f"You need at least 50 reward points to cast a vote as a juror, but you have {user_profile.rewards} points.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a verdict.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for = get_object_or_404(User, id=voted_for_id)
    if voted_for != task.posted_by and voted_for != task.taken_by:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= 50
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-50,
            transaction_type='juror_stake_lock',
            description=f"Locked 50 points stake for juror vote on dispute for task: '{task.title}'"
        )

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for,
            stake_amount=50
        )

    messages.success(request, f"Your vote for {voted_for.username} has been cast and 50 points locked as stake.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def resolve_dispute(dispute):
    """
    Resolves a dispute based on majority juror votes, slashes losing party and minority jurors,
    and redistributes slashed pool (30% to winner, 70% to majority jurors).
    """
    dispute.refresh_from_db()
    task = dispute.task
    votes = dispute.votes.all()
    if not votes.exists():
        return False

    with transaction.atomic():
        worker_votes = votes.filter(voted_for=task.taken_by)
        poster_votes = votes.filter(voted_for=task.posted_by)

        worker_bond = dispute.worker_deposit_amount or (dispute.deposit_amount if dispute.worker_deposited else 0)
        poster_bond = dispute.poster_deposit_amount or (dispute.deposit_amount if dispute.poster_deposited else 0)

        if worker_votes.count() > poster_votes.count():
            winner = task.taken_by
            loser = task.posted_by
            winning_votes = worker_votes
            losing_votes = poster_votes
            winner_is_worker = True
            losing_bond = poster_bond
            winner_bond = worker_bond
        else:
            winner = task.posted_by
            loser = task.taken_by
            winning_votes = poster_votes
            losing_votes = worker_votes
            winner_is_worker = False
            losing_bond = worker_bond
            winner_bond = poster_bond

        minority_juror_count = losing_votes.count()
        minority_stakes = minority_juror_count * 50
        total_slashed_pool = losing_bond + minority_stakes

        winner_profile = winner.userprofile
        # Winner recovers original deposit bond
        winner_profile.rewards += winner_bond
        RewardLedger.objects.create(
            user=winner,
            task=task,
            amount=winner_bond,
            transaction_type='dispute_refund',
            description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
        )

        # Winner recovers 30% of slashed pool
        winner_cut = math.floor(total_slashed_pool * 0.30)
        if winner_cut > 0:
            winner_profile.rewards += winner_cut
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=winner_cut,
                transaction_type='dispute_refund',
                description=f"Awarded 30% dividend share of slashed dispute pool for task: '{task.title}'"
            )

        # Settle task rewards
        if winner_is_worker:
            winner_profile.rewards += task.reward
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Awarded task reward for winning dispute on task: '{task.title}'"
            )
            task.status = 'completed'
        else:
            winner_profile.rewards += task.reward
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refunded task reward for winning dispute on task: '{task.title}'"
            )
            task.status = 'cancelled'

        winner_profile.save()
        task.save()

        # Majority jurors receive original 50 stake refund + equal split of remaining 70% slashed pool
        majority_count = winning_votes.count()
        majority_pool = total_slashed_pool - winner_cut
        share_per_juror = (majority_pool // majority_count) if majority_count > 0 else 0

        for vote in winning_votes:
            juror = vote.voter
            juror_profile = juror.userprofile
            total_payout = 50 + share_per_juror
            juror_profile.rewards += total_payout
            juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=total_payout,
                transaction_type='juror_reward',
                description=f"Returned 50 stake plus dividend share ({share_per_juror} pts) for majority vote on dispute: '{task.title}'"
            )

        # Minority jurors stakes are slashed (100% loss)
        for vote in losing_votes:
            juror = vote.voter
            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Slashed 100% of 50 pt stake for minority vote on dispute: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.escrow_status = 'forfeited'
        dispute.save()

        # Record forfeit entry for loser
        RewardLedger.objects.create(
            user=loser,
            task=task,
            amount=0,
            transaction_type='dispute_forfeit',
            description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'"
        )

        return True

@login_required(login_url='/login/')
@require_POST
def resolve_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    if resolve_dispute(dispute):
        messages.success(request, "Dispute resolved based on jury votes.")
    else:
        messages.error(request, "Cannot resolve dispute without jury votes.")
    return redirect('dispute_detail', dispute_id=dispute.id)
