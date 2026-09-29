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

    # Check if voting deadline expired for an open dispute
    if dispute.status == 'open' and dispute.is_voting_expired():
        dispute.tally_and_settle()
        dispute.refresh_from_db()

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote = dispute.can_user_vote(request.user)
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first() if request.user.is_authenticated else None

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'poster_votes': dispute.poster_votes_count(),
        'taker_votes': dispute.taker_votes_count(),
        'total_votes': dispute.total_votes_count(),
        'quorum_target': dispute.get_quorum_target(),
        'voting_deadline': dispute.get_voting_deadline(),
        'is_expired': dispute.is_voting_expired(),
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for community voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.is_voting_expired():
        dispute.tally_and_settle()
        messages.error(request, "The voting window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if hasattr(request.user, 'userprofile') and request.user.userprofile.reputation_score < 50:
        messages.error(request, "Your reputation score does not meet the minimum threshold to vote as a juror.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute_obj = Dispute.objects.select_for_update().get(id=dispute.id)
        if dispute_obj.status != 'open':
            messages.error(request, "This dispute is no longer open for voting.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        DisputeVote.objects.create(
            dispute=dispute_obj,
            voter=request.user,
            vote=vote_choice
        )

        if dispute_obj.total_votes_count() >= dispute_obj.get_quorum_target():
            dispute_obj.tally_and_settle()
            messages.success(request, "Your vote was recorded and reached quorum! The dispute has been automatically settled based on jury consensus.")
        else:
            messages.success(request, "Your vote has been recorded. Thank you for participating in the community jury!")

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
