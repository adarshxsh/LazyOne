from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JuryVote
from django.views.decorators.http import require_POST
from django.urls import reverse

CONSENSUS_THRESHOLD = 3

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    user_vote = JuryVote.objects.filter(dispute=dispute, voter=request.user).first()
    has_voted = user_vote is not None
    poster_votes = dispute.jury_votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.jury_votes.filter(voted_for=task.taken_by).count()
    total_votes = poster_votes + taker_votes

    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote = (dispute.status == 'open' and not is_party and not has_voted)

    context = {
        'dispute': dispute,
        'task': task,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'consensus_threshold': CONSENSUS_THRESHOLD,
        'is_party': is_party,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and task takers in a dispute cannot act as jurors for that dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JuryVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for_id')
    try:
        voted_for_id_int = int(voted_for_id) if voted_for_id else None
    except (ValueError, TypeError):
        voted_for_id_int = None

    if not voted_for_id_int or voted_for_id_int not in [task.posted_by.id, task.taken_by.id]:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id_int)

    with transaction.atomic():
        JuryVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for_user
        )
        messages.success(request, f"Your vote in favor of {voted_for_user.username} has been recorded.")
        resolve_dispute_if_consensus(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)

def resolve_dispute_if_consensus(dispute, threshold=CONSENSUS_THRESHOLD):
    if dispute.status != 'open':
        return

    votes = dispute.jury_votes.all()
    if votes.count() < threshold:
        return

    task = dispute.task
    poster_votes = votes.filter(voted_for=task.posted_by).count()
    taker_votes = votes.filter(voted_for=task.taken_by).count()

    if poster_votes > taker_votes:
        winning_user = task.posted_by
        losing_user = task.taken_by
    else:
        winning_user = task.taken_by
        losing_user = task.posted_by

    dispute.status = 'resolved'
    bond_amount = dispute.deposit_amount

    if losing_user == dispute.raised_by:
        dispute.escrow_status = 'forfeited'
        RewardLedger.objects.create(
            user=losing_user,
            task=task,
            amount=0,
            transaction_type='dispute_penalty',
            description=f"Dispute penalty: deposit bond forfeited for task: '{task.title}'"
        )
    else:
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
        )
        losing_user_profile = losing_user.userprofile
        losing_user_profile.rewards -= bond_amount
        losing_user_profile.save()
        RewardLedger.objects.create(
            user=losing_user,
            task=task,
            amount=-bond_amount,
            transaction_type='dispute_penalty',
            description=f"Dispute penalty assessed for task: '{task.title}'"
        )

    if winning_user == task.taken_by:
        task.status = 'completed'
        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += task.reward
        taker_profile.save()
        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=task.reward,
            transaction_type='task_completion',
            description=f"Completed task via dispute resolution for task: '{task.title}'"
        )
    else:
        task.status = 'cancelled'
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Refund for disputed task: '{task.title}' resolved in favor of poster"
        )

    task.save()
    dispute.save()

    winning_votes = votes.filter(voted_for=winning_user)
    if winning_votes.exists() and bond_amount > 0:
        share = bond_amount // winning_votes.count()
        for vote in winning_votes:
            juror_profile = vote.voter.userprofile
            juror_profile.rewards += share
            juror_profile.save()
            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=share,
                transaction_type='juror_reward',
                description=f"Juror reward for resolving dispute on task: '{task.title}'"
            )

    Notification.objects.create(
        recipient=winning_user,
        message=f"Dispute for task '{task.title}' has been resolved in your favor.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    Notification.objects.create(
        recipient=losing_user,
        message=f"Dispute for task '{task.title}' has been resolved against you.",
        link=reverse('dispute_detail', args=[dispute.id])
    )

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
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
