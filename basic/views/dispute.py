import math
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()
    total_votes = dispute.votes.count()

    can_counter_deposit = (
        dispute.status == 'open' and 
        not dispute.has_counter_deposit and 
        request.user == dispute.counter_party and 
        request.user.userprofile.rewards >= task.deposit_bond_amount
    )
    can_vote = dispute.can_vote(request.user)

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'can_counter_deposit': can_counter_deposit,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if (request.user != task.taken_by and request.user != task.posted_by) or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you posted or took that is currently in progress.")
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

            if request.user == task.posted_by:
                poster_amt = deposit_amount
                poster_esc = 'held'
                worker_amt = 0
                worker_esc = 'pending'
                counter_party = task.taken_by
            else:
                worker_amt = deposit_amount
                worker_esc = 'held'
                poster_amt = 0
                poster_esc = 'pending'
                counter_party = task.posted_by

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.worker_deposit_amount = worker_amt
                dispute.worker_escrow_status = worker_esc
                dispute.poster_deposit_amount = poster_amt
                dispute.poster_escrow_status = poster_esc
                dispute.counter_bond_deadline = deadline
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    worker_deposit_amount=worker_amt,
                    worker_escrow_status=worker_esc,
                    poster_deposit_amount=poster_amt,
                    poster_escrow_status=poster_esc,
                    counter_bond_deadline=deadline
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

            if counter_party:
                Notification.objects.create(
                    recipient=counter_party,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'. Matching counter-deposit bond required within 48 hours.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def post_counter_deposit(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "Dispute is not open.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.has_counter_deposit:
        messages.info(request, "Matching counter-deposit has already been posted.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user != dispute.counter_party:
        messages.error(request, "Only the counter-party can post the matching deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    required_bond = task.deposit_bond_amount
    user_profile = request.user.userprofile

    if user_profile.rewards < required_bond:
        messages.error(request, f"Insufficient balance to post counter-deposit. Required: {required_bond} points.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= required_bond
        user_profile.save()

        if request.user == task.posted_by:
            dispute.poster_deposit_amount = required_bond
            dispute.poster_escrow_status = 'held'
        else:
            dispute.worker_deposit_amount = required_bond
            dispute.worker_escrow_status = 'held'
        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-required_bond,
            transaction_type='dispute_counter_deposit',
            description=f"Counter deposit bond posted for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=dispute.raised_by,
            message=f"{request.user.username} has posted matching counter-deposit bond for dispute on task '{task.title}'. Community voting is now active.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Counter-deposit bond of {required_bond} points posted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if not dispute.can_vote(request.user):
        messages.error(request, "You are not eligible to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker', 'worker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if choice == 'worker':
        choice = 'taker'

    with transaction.atomic():
        user_profile = request.user.userprofile
        user_profile.rewards -= 20
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-20,
            transaction_type='juror_stake',
            description=f"Juror stake locked for vote on dispute for task: '{task.title}'"
        )

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            choice=choice,
            stake=20,
            status='staked'
        )

        if dispute.votes.count() >= 11:
            resolve_dispute_instance(dispute)

    messages.success(request, "Your vote and 20-point stake have been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def resolve_dispute_instance(dispute, winning_choice=None):
    dispute.refresh_from_db()
    task = dispute.task
    with transaction.atomic():
        if not dispute.has_counter_deposit:
            dispute.status = 'resolved'
            if dispute.raised_by == task.posted_by:
                dispute.refund_poster_deposit(reason_description=f"Deposit bond refunded for auto-resolved dispute on task: '{task.title}'")
                task.status = 'cancelled'
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for task reward on default dispute win for task: '{task.title}'"
                )
            else:
                dispute.refund_worker_deposit(reason_description=f"Deposit bond refunded for auto-resolved dispute on task: '{task.title}'")
                task.status = 'completed'
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded task reward on default dispute win for task: '{task.title}'"
                    )
            dispute.save()
            task.save()

            for user in [task.posted_by, task.taken_by]:
                if user:
                    Notification.objects.create(
                        recipient=user,
                        message=f"Dispute for task '{task.title}' resolved in favor of {dispute.raised_by.username} (no counter deposit).",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
            return

        if not winning_choice:
            poster_votes = dispute.votes.filter(choice='poster').count()
            taker_votes = dispute.votes.filter(choice='taker').count()
            if poster_votes > taker_votes:
                winning_choice = 'poster'
            elif taker_votes > poster_votes:
                winning_choice = 'taker'
            else:
                winning_choice = 'poster' if dispute.raised_by == task.posted_by else 'taker'

        if winning_choice == 'poster':
            dispute.refund_poster_deposit(reason_description=f"Poster deposit bond refunded for won dispute on task: '{task.title}'")
            dispute.forfeit_worker_deposit(beneficiary=task.posted_by, reason_description=f"Worker forfeited deposit bond awarded to poster for won dispute on task: '{task.title}'")
            task.status = 'cancelled'
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for task reward on won dispute for task: '{task.title}'"
            )
        else:
            dispute.refund_worker_deposit(reason_description=f"Worker deposit bond refunded for won dispute on task: '{task.title}'")
            dispute.forfeit_poster_deposit(beneficiary=task.taken_by, reason_description=f"Poster forfeited deposit bond awarded to worker for won dispute on task: '{task.title}'")
            task.status = 'completed'
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded task reward on won dispute for task: '{task.title}'"
                )

        majority_votes = list(dispute.votes.filter(choice=winning_choice))
        minority_votes = list(dispute.votes.exclude(choice=winning_choice))

        total_minority_slashed = len(minority_votes) * 20

        for vote in minority_votes:
            vote.status = 'slashed'
            vote.save()
            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Juror stake slashed for minority vote on dispute for task: '{task.title}'"
            )

        majority_count = len(majority_votes)
        share = math.floor(total_minority_slashed / majority_count) if majority_count > 0 else 0

        for vote in majority_votes:
            vote.status = 'rewarded'
            vote.save()
            return_amount = 20 + share
            juror_profile = vote.voter.userprofile
            juror_profile.rewards += return_amount
            juror_profile.save()
            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=return_amount,
                transaction_type='juror_reward',
                description=f"Juror stake returned (20 pts) plus reward share ({share} pts) for majority vote on dispute for task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()
        task.save()

        for user in [task.posted_by, task.taken_by]:
            if user:
                Notification.objects.create(
                    recipient=user,
                    message=f"Dispute for task '{task.title}' has been resolved.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

@login_required(login_url='/login/')
@require_POST
def resolve_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status == 'open':
        resolve_dispute_instance(dispute)
        messages.success(request, f"Dispute for '{dispute.task.title}' has been resolved.")
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

        counter_party = dispute.counter_party
        if counter_party:
            Notification.objects.create(
                recipient=counter_party,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
