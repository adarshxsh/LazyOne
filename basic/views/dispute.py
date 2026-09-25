from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

VOTE_THRESHOLD = 3

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    if not is_participant and not request.user.is_staff and dispute.status != 'voting':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_list = dispute.evidence_records.order_by('created_at')
    votes_list = dispute.votes.order_by('-created_at')
    user_has_voted = dispute.votes.filter(voter=request.user).exists()

    poster_votes_count = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes_count = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'evidence_list': evidence_list,
        'votes_list': votes_list,
        'user_has_voted': user_has_voted,
        'poster_votes_count': poster_votes_count,
        'taker_votes_count': taker_votes_count,
        'vote_threshold': VOTE_THRESHOLD,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['evidence_submission', 'voting']:
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user and task.posted_by != request.user:
        messages.error(request, "You can only raise a dispute for a task you posted or took.")
        return redirect('my_tasks')
    if task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task currently in progress.")
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
                dispute.status = 'evidence_submission'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='evidence_submission',
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            DisputeEvidence.objects.create(
                dispute=dispute,
                submitted_by=request.user,
                title="Initial Dispute Statement",
                description=reason
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

            recipient = task.posted_by if request.user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can submit evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'evidence_submission':
        messages.error(request, "Evidence submission is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    title = request.POST.get('title', 'Evidence Record').strip()
    description = request.POST.get('description', '').strip()
    file_attachment = request.FILES.get('file_attachment')

    if not description:
        messages.error(request, "Evidence description cannot be empty.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        title=title or "Evidence Record",
        description=description,
        file_attachment=file_attachment
    )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def advance_to_voting(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to transition this dispute phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status == 'evidence_submission':
        dispute.status = 'voting'
        dispute.save()
        messages.success(request, "Dispute transitioned to peer jury voting phase.")
    else:
        messages.error(request, "Dispute cannot transition to voting from current status.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote as peer jurors on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'voting':
        messages.error(request, "Voting is not currently active for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    reason = request.POST.get('reason', '').strip()

    if not reason:
        messages.error(request, "Please provide a reason for your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if str(task.posted_by.id) == voted_for_id:
        voted_for = task.posted_by
    elif task.taken_by and str(task.taken_by.id) == voted_for_id:
        voted_for = task.taken_by
    else:
        messages.error(request, "Invalid vote target.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for,
            reason=reason
        )

        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
        total_votes = poster_votes + taker_votes

        if total_votes >= VOTE_THRESHOLD:
            if poster_votes > taker_votes:
                winner = task.posted_by
            elif taker_votes > poster_votes:
                winner = task.taken_by
            else:
                winner = None

            if winner:
                dispute.resolve_dispute(winner, reason_description=f"Resolved via peer jury consensus vote for {winner.username}")
                messages.success(request, f"Vote recorded. Consensus threshold reached! Dispute resolved in favor of {winner.username}.")
            else:
                messages.success(request, "Vote recorded.")
        else:
            messages.success(request, "Vote submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task

    if dispute.status in ['resolved', 'withdrawn']:
        messages.error(request, "Dispute is already closed.")
        return redirect('my_tasks')

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
