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

    is_assigned_juror = dispute.assigned_jurors.filter(id=request.user.id).exists()
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    if not (is_participant or request.user.is_staff or is_assigned_juror):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    has_voted = dispute.votes.filter(voter=request.user).exists()
    can_vote = (dispute.status == 'open' and is_assigned_juror and not is_participant and not has_voted)

    poster_votes = dispute.votes.filter(vote_choice='poster').count()
    worker_votes = dispute.votes.filter(vote_choice='worker').count()
    total_votes = dispute.votes.count()
    assigned_jurors_count = dispute.assigned_jurors.count()

    user_vote = dispute.votes.filter(voter=request.user).first() if has_voted else None
    votes_list = dispute.votes.select_related('voter').all()

    context = {
        'dispute': dispute,
        'task': task,
        'is_assigned_juror': is_assigned_juror,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'worker_votes': worker_votes,
        'total_votes': total_votes,
        'assigned_jurors_count': assigned_jurors_count,
        'votes_list': votes_list,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Task poster and worker strictly blocked from voting on direct tasks
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster or worker cannot vote on their own task dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # User must be an assigned juror
    if not dispute.assigned_jurors.filter(id=request.user.id).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Dispute must be open
    if dispute.status != 'open':
        messages.error(request, "Votes cannot be submitted or changed once a dispute reaches resolved status.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Prevent duplicate votes
    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice', '').strip().lower()
    comment = request.POST.get('comment', '').strip()

    if vote_choice not in ['poster', 'worker']:
        messages.error(request, "Invalid vote choice. Please select Poster or Worker.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote_choice=vote_choice,
            comment=comment
        )

        poster_votes = dispute.votes.filter(vote_choice='poster').count()
        worker_votes = dispute.votes.filter(vote_choice='worker').count()
        total_votes = dispute.votes.count()
        total_jurors = dispute.assigned_jurors.count()

        majority_needed = (total_jurors // 2) + 1 if total_jurors > 0 else 1

        if poster_votes >= majority_needed or (total_jurors > 0 and total_votes >= total_jurors and poster_votes > worker_votes):
            _resolve_dispute(dispute, winner='poster')
            messages.success(request, "Vote recorded successfully. Dispute resolved in favor of the poster by majority consensus.")
        elif worker_votes >= majority_needed or (total_jurors > 0 and total_votes >= total_jurors and worker_votes > poster_votes):
            _resolve_dispute(dispute, winner='worker')
            messages.success(request, "Vote recorded successfully. Dispute resolved in favor of the worker by majority consensus.")
        else:
            messages.success(request, "Your vote has been submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)

def _resolve_dispute(dispute, winner):
    task = dispute.task
    if winner == 'worker':
        task.status = 'completed'
        task.save()

        worker = task.taken_by
        if worker:
            worker_profile = worker.userprofile
            worker_profile.rewards += task.reward
            worker_profile.save()

            RewardLedger.objects.create(
                user=worker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task completed via dispute consensus: '{task.title}'"
            )

        if dispute.raised_by == worker:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded following dispute consensus victory on task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=worker,
                reason_description=f"Security deposit bond forfeited to worker following dispute consensus victory for worker on task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved in favor of the worker via community vote.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if worker:
            Notification.objects.create(
                recipient=worker,
                message=f"Dispute for task '{task.title}' resolved in your favor via community vote.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    elif winner == 'poster':
        task.status = 'cancelled'
        task.save()

        poster = task.posted_by
        poster_profile = poster.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=poster,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Task points refunded following dispute consensus resolution on task: '{task.title}'"
        )

        if dispute.raised_by == poster:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded following dispute consensus victory on task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=poster,
                reason_description=f"Security deposit bond forfeited to poster following dispute consensus victory for poster on task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()

        Notification.objects.create(
            recipient=poster,
            message=f"Dispute for task '{task.title}' resolved in your favor via community vote.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved in favor of the poster via community vote.",
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
