from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, DisputeVote, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

def settle_dispute_by_vote(dispute):
    task = dispute.task
    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()

    if poster_votes > taker_votes:
        winner = 'poster'
    else:
        winner = 'taker'

    if winner == 'poster':
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Task reward refunded via peer vote consensus on dispute for task: '{task.title}'"
        )

        if dispute.escrow_status == 'held':
            if dispute.raised_by == task.taken_by:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Deposit bond forfeited to poster via peer vote settlement for task: '{task.title}'"
                )
            else:
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded to poster via peer vote settlement for task: '{task.title}'"
                )

        task.status = 'cancelled'
        task.save()

        dispute.status = 'resolved'
        dispute.save()

        msg = f"Dispute for task '{task.title}' has been settled by peer consensus in favor of the poster ({poster_votes}-{taker_votes})."
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(recipient=task.posted_by, message=msg, link=dispute_link)
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(recipient=task.taken_by, message=msg, link=dispute_link)

    else:
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded via peer vote consensus on dispute for task: '{task.title}'"
            )

        if dispute.escrow_status == 'held':
            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded to taker via peer vote settlement for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.taken_by,
                    reason_description=f"Deposit bond forfeited to taker via peer vote settlement for task: '{task.title}'"
                )

        task.status = 'completed'
        task.save()

        dispute.status = 'resolved'
        dispute.save()

        msg = f"Dispute for task '{task.title}' has been settled by peer consensus in favor of the taker ({taker_votes}-{poster_votes})."
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(recipient=task.posted_by, message=msg, link=dispute_link)
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(recipient=task.taken_by, message=msg, link=dispute_link)

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()
    total_votes = poster_votes + taker_votes
    quorum_threshold = 5
    quorum_progress = min(100, int((total_votes / quorum_threshold) * 100))

    user_voted = dispute.votes.filter(voter=request.user).exists()
    user_vote = dispute.votes.filter(voter=request.user).first() if user_voted else None
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote = (dispute.status == 'open' and not is_participant and not user_voted)

    votes = dispute.votes.select_related('voter').order_by('-created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'quorum_threshold': quorum_threshold,
        'quorum_progress': quorum_progress,
        'user_voted': user_voted,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
        'votes': votes,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    with transaction.atomic():
        dispute = get_object_or_404(Dispute.objects.select_for_update(), id=dispute_id)
        task = dispute.task

        if dispute.status != 'open':
            messages.error(request, "This dispute is no longer open for voting.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if request.user == task.posted_by or request.user == task.taken_by:
            messages.error(request, "Task posters and takers cannot vote on their own dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
            messages.error(request, "You have already voted on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        vote_choice = request.POST.get('vote')
        if vote_choice not in ['poster', 'taker']:
            messages.error(request, "Please select a valid vote choice (Poster or Taker).")
            return redirect('dispute_detail', dispute_id=dispute.id)

        rationale = request.POST.get('rationale', '').strip()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote=vote_choice,
            rationale=rationale
        )

        total_votes = dispute.votes.count()
        if total_votes >= 5:
            settle_dispute_by_vote(dispute)
            messages.success(request, f"Your vote has been recorded. Quorum threshold reached ({total_votes}/5)! Dispute has been settled.")
        else:
            messages.success(request, f"Your vote has been recorded successfully ({total_votes}/5 votes towards quorum).")

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
