from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction, IntegrityError
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

QUORUM_THRESHOLD = 5

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    has_voted = DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists()

    if dispute.status != 'open' and not is_participant and not request.user.is_staff and not has_voted:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    total_votes = dispute.votes.count()
    poster_votes = dispute.votes.filter(vote='posted_by').count()
    worker_votes = dispute.votes.filter(vote='taken_by').count()

    poster_percent = int((poster_votes / total_votes) * 100) if total_votes > 0 else 0
    worker_percent = int((worker_votes / total_votes) * 100) if total_votes > 0 else 0

    user_vote = dispute.votes.filter(voter=request.user).first() if has_voted else None
    can_vote = (dispute.status == 'open' and not is_participant and not has_voted)

    context = {
        'dispute': dispute,
        'task': task,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'poster_percent': poster_percent,
        'worker_percent': worker_percent,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
        'quorum_threshold': QUORUM_THRESHOLD,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "You are not authorized to vote on your own task dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote') or request.POST.get('choice')
    if vote_choice not in ['posted_by', 'taken_by']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '')

    with transaction.atomic():
        try:
            DisputeVote.objects.create(
                dispute=dispute,
                voter=request.user,
                vote=vote_choice,
                reason=reason
            )
        except IntegrityError:
            messages.error(request, "You have already cast a vote on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        total_votes = dispute.votes.count()
        if total_votes >= QUORUM_THRESHOLD:
            poster_votes = dispute.votes.filter(vote='posted_by').count()
            worker_votes = dispute.votes.filter(vote='taken_by').count()

            if worker_votes > poster_votes:
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded via community vote consensus for task: '{task.title}'"
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
                        description=f"Awarded task reward via community vote consensus on dispute: '{task.title}'"
                    )
                task.status = 'completed'
                dispute.status = 'resolved'
                task.save()
                dispute.save()

                msg = f"Quorum of {QUORUM_THRESHOLD} votes reached! Dispute resolved in favor of worker. Deposit bond refunded and task completed."
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Deposit bond forfeited via community vote consensus for task: '{task.title}'"
                )
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refunded task reward via community vote consensus on dispute: '{task.title}'"
                )
                task.status = 'cancelled'
                dispute.status = 'resolved'
                task.save()
                dispute.save()

                msg = f"Quorum of {QUORUM_THRESHOLD} votes reached! Dispute resolved in favor of poster. Deposit bond awarded to poster and task cancelled."

            dispute_link = reverse('dispute_detail', args=[dispute.id])
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved by community consensus.",
                link=dispute_link
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved by community consensus.",
                    link=dispute_link
                )

            messages.success(request, msg)
        else:
            messages.success(request, "Your vote has been submitted successfully.")

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
