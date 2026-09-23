from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_participant = dispute.is_participant(request.user)
    is_staff = request.user.is_staff
    is_voting_phase = dispute.status == 'voting'
    
    if not is_participant and not is_staff and not is_voting_phase:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidences = dispute.evidences.select_related('submitted_by').all()
    votes = dispute.votes.select_related('voter', 'voted_for').all()
    
    poster_votes_count = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes_count = dispute.votes.filter(voted_for=task.taken_by).count()
    
    user_vote = dispute.votes.filter(voter=request.user).first() if request.user.is_authenticated else None
    is_eligible_voter = dispute.is_eligible_voter(request.user)

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'votes': votes,
        'is_participant': is_participant,
        'is_eligible_voter': is_eligible_voter,
        'user_vote': user_vote,
        'poster_votes_count': poster_votes_count,
        'taker_votes_count': taker_votes_count,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status != 'resolved':
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

            DisputeEvidence.objects.create(
                dispute=dispute,
                submitted_by=request.user,
                text_evidence=reason
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
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not dispute.is_participant(request.user) and not request.user.is_staff:
        messages.error(request, "Only task participants can submit evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status not in ['open', 'evidence_submission']:
        messages.error(request, "Evidence submission phase is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    text_evidence = request.POST.get('text_evidence', '').strip()
    file_evidence = request.FILES.get('file_evidence')

    if not text_evidence and not file_evidence:
        messages.error(request, "Please provide text evidence or upload a file.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=request.user,
            text_evidence=text_evidence,
            file_evidence=file_evidence
        )
        if dispute.status == 'open':
            dispute.status = 'evidence_submission'
            dispute.save()

        other_user = dispute.task.posted_by if request.user == dispute.task.taken_by else dispute.task.taken_by
        if other_user:
            Notification.objects.create(
                recipient=other_user,
                message=f"New evidence submitted by {request.user.username} for dispute on task: '{dispute.task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def advance_dispute_phase(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not dispute.is_participant(request.user) and not request.user.is_staff:
        messages.error(request, "Only task participants or staff can advance dispute phases.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    target_status = request.POST.get('target_status')
    if not target_status:
        next_phases = {
            'open': 'evidence_submission',
            'evidence_submission': 'under_review',
            'under_review': 'voting',
            'voting': 'resolved'
        }
        target_status = next_phases.get(dispute.status)

    if not target_status or not dispute.can_transition_to(target_status):
        messages.error(request, f"Cannot transition dispute from state '{dispute.get_status_display()}' to '{target_status}'.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if target_status == 'resolved':
            dispute.resolve_dispute(reason_description=f"Dispute resolved manually during phase advance for task: '{dispute.task.title}'")
        else:
            dispute.transition_to(target_status)

        participants = [dispute.task.posted_by]
        if dispute.task.taken_by and dispute.task.taken_by not in participants:
            participants.append(dispute.task.taken_by)

        for participant in participants:
            if participant != request.user:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Dispute for task '{dispute.task.title}' has advanced to '{dispute.get_status_display()}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    messages.success(request, f"Dispute status advanced to {dispute.get_status_display()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.status != 'voting':
        messages.error(request, "Voting is only allowed during the voting phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.is_participant(request.user):
        messages.error(request, "Task participants are not eligible to vote in their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.votes.filter(voter=request.user).exists():
        messages.error(request, "You have already cast a vote in this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id or str(voted_for_id) not in [str(dispute.task.posted_by.id), str(dispute.task.taken_by.id) if dispute.task.taken_by else '']:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    comment = request.POST.get('comment', '').strip()

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for_id=voted_for_id,
            comment=comment
        )

    messages.success(request, "Your vote has been submitted successfully!")
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

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
