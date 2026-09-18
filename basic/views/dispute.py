from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeEvidence, DisputeVote, Task, Notification, RewardLedger, UserProfile

ALLOWED_EVIDENCE_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'txt'}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_participant = request.user in [task.posted_by, task.taken_by]
    
    evidence_list = dispute.evidence_entries.all().order_by('-uploaded_at')
    votes = dispute.votes.all().order_by('-voted_at')
    posted_by_votes = dispute.votes.filter(vote='posted_by').count()
    taken_by_votes = dispute.votes.filter(vote='taken_by').count()
    total_votes = dispute.votes.count()
    
    user_vote = None
    if request.user.is_authenticated:
        user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()

    can_upload_evidence = is_participant and dispute.status == 'evidence_submission'
    can_transition_to_voting = is_participant and dispute.status == 'evidence_submission'
    can_vote = (not is_participant) and request.user.is_authenticated and (dispute.status == 'jury_voting') and (user_vote is None)

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_list': evidence_list,
        'votes': votes,
        'posted_by_votes': posted_by_votes,
        'taken_by_votes': taken_by_votes,
        'total_votes': total_votes,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_upload_evidence': can_upload_evidence,
        'can_transition_to_voting': can_transition_to_voting,
        'can_vote': can_vote,
        'vote_threshold': 3,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
        return redirect('my_tasks')
    
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
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
                if dispute.status in ['evidence_submission', 'jury_voting']:
                    messages.info(request, "An active dispute already exists for this task.")
                    return redirect('dispute_detail', dispute_id=dispute.id)
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

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. You can now upload evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def upload_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can upload evidence for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'evidence_submission':
        messages.error(request, "Evidence can only be uploaded during the evidence submission phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '')
    files = request.FILES.getlist('file') or request.FILES.getlist('evidence_files')
    if not files and 'file' in request.FILES:
        files = [request.FILES['file']]

    if not files:
        messages.error(request, "Please select at least one file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    uploaded_count = 0
    for file in files:
        if file.size > MAX_FILE_SIZE_BYTES:
            messages.error(request, f"File '{file.name}' exceeds the maximum allowed size of 10 MB.")
            continue
        ext = file.name.split('.')[-1].lower() if '.' in file.name else ''
        if ext not in ALLOWED_EVIDENCE_EXTENSIONS:
            messages.error(request, f"File '{file.name}' has unsupported format. Allowed formats: PDF, PNG, JPG, JPEG, TXT.")
            continue

        DisputeEvidence.objects.create(
            dispute=dispute,
            uploaded_by=request.user,
            file=file,
            description=description
        )
        uploaded_count += 1

    if uploaded_count > 0:
        messages.success(request, f"Successfully uploaded {uploaded_count} evidence file(s).")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def transition_to_jury_voting(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can send a dispute to jury voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'evidence_submission':
        messages.error(request, "Dispute is not currently in evidence submission state.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.status = 'jury_voting'
    dispute.save()

    counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"Dispute for task '{task.title}' has transitioned to community jury voting.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Dispute moved to jury voting phase.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'jury_voting':
        messages.error(request, "This dispute is not open for jury voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user in [task.posted_by, task.taken_by]:
        messages.error(request, "Task participants cannot vote as jurors on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    rationale = request.POST.get('rationale', '')

    if vote_choice not in ['posted_by', 'taken_by']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        vote=vote_choice,
        rationale=rationale
    )

    messages.success(request, "Your vote has been submitted.")

    # Check for threshold consensus (e.g. 3 votes)
    total_votes = dispute.votes.count()
    if total_votes >= 3:
        posted_by_votes = dispute.votes.filter(vote='posted_by').count()
        taken_by_votes = dispute.votes.filter(vote='taken_by').count()

        with transaction.atomic():
            if posted_by_votes > taken_by_votes:
                # Poster wins: task cancelled, points refunded
                winner = task.posted_by
                dispute.status = 'resolved_posted_by'
                task.status = 'cancelled'
                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_refund',
                    description=f"Refund from dispute resolution for task '{task.title}'"
                )
                resolution_msg = f"Dispute resolved in favor of task poster {task.posted_by.username}. {task.reward} points refunded to poster."
            else:
                # Taker wins: task completed, reward transferred to taker
                winner = task.taken_by
                dispute.status = 'resolved_taken_by'
                task.status = 'completed'
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_payout',
                    description=f"Awarded from dispute resolution for task '{task.title}'"
                )
                resolution_msg = f"Dispute resolved in favor of task taker {task.taken_by.username}. {task.reward} points awarded to taker."

            if dispute.raised_by == winner:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded following dispute resolution in favor of {winner.username}.")
            else:
                dispute.forfeit_deposit(beneficiary=winner, reason_description=f"Security deposit bond forfeited to {winner.username} following dispute resolution.")

            dispute.save()
            task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' has been finalized by community jury: {resolution_msg}",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' has been finalized by community jury: {resolution_msg}",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by, dispute.raised_by]:
        messages.error(request, "You are not authorized to withdraw this dispute.")
        return redirect('my_tasks')

    if dispute.status in ['resolved_posted_by', 'resolved_taken_by']:
        messages.error(request, "Cannot withdraw a resolved dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
