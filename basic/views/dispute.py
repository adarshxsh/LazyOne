from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Check if open dispute should be auto-resolved due to deadline
    dispute.check_and_resolve()

    user_vote = DisputeVote.objects.filter(voter=request.user, dispute=dispute).first()
    user_voted = user_vote is not None
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote = (not is_participant) and (not user_voted) and (dispute.status == 'open')

    total_votes = dispute.total_votes_count
    poster_votes = dispute.poster_votes_count
    taker_votes = dispute.taker_votes_count
    quorum_target = dispute.quorum_target

    if total_votes > 0:
        poster_percentage = round((poster_votes / total_votes) * 100)
        taker_percentage = round((taker_votes / total_votes) * 100)
    else:
        poster_percentage = 0
        taker_percentage = 0

    quorum_percentage = min(100, round((total_votes / quorum_target) * 100))

    context = {
        'dispute': dispute,
        'task': task,
        'user_voted': user_voted,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'is_participant': is_participant,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'quorum_target': quorum_target,
        'poster_percentage': poster_percentage,
        'taker_percentage': taker_percentage,
        'quorum_percentage': quorum_percentage,
        'voting_deadline_iso': dispute.voting_deadline.isoformat(),
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and taker are strictly barred from voting on their own disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(voter=request.user, dispute=dispute).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    chosen_party = request.POST.get('chosen_party') or request.POST.get('vote')
    rationale = request.POST.get('rationale', '')

    if chosen_party not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice. Please select either the Task Poster or Task Taker.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        voter=request.user,
        dispute=dispute,
        chosen_party=chosen_party,
        rationale=rationale,
        vote_weight=1
    )

    messages.success(request, "Your jury vote has been submitted successfully.")

    # Check if vote triggers quorum or automated resolution
    resolved = dispute.check_and_resolve()
    if resolved:
        messages.info(request, "Jury vote quorum reached! The dispute has been automatically resolved.")

    return redirect('dispute_detail', dispute_id=dispute.id)


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
