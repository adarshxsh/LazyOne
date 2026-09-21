from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, JurorCommitment, JurorVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    user_commitment = JurorCommitment.objects.filter(dispute=dispute, juror=request.user).first()
    user_vote = JurorVote.objects.filter(dispute=dispute, juror=request.user).first()

    context = {
        'dispute': dispute,
        'task': task,
        'phase': dispute.phase,
        'user_commitment': user_commitment,
        'user_vote': user_vote,
        'has_committed': user_commitment is not None,
        'has_revealed': user_vote is not None and user_vote.is_verified,
        'is_concluded': dispute.phase == 'concluded' or dispute.status == 'resolved',
    }

    # Only show vote counts / tally when phase is concluded
    if dispute.phase == 'concluded' or dispute.status == 'resolved':
        verified_votes = dispute.votes.filter(is_verified=True)
        context['poster_votes'] = verified_votes.filter(choice='poster').count()
        context['taker_votes'] = verified_votes.filter(choice='taker').count()
        context['total_votes'] = verified_votes.count()

    return render(request, 'dispute_detail.html', context)

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
                dispute.phase = 'commit'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='open',
                    phase='commit',
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
        dispute.phase = 'concluded'
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

@login_required(login_url='/login/')
@require_POST
def submit_commitment(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.phase != 'commit' or dispute.status != 'open':
        messages.error(request, "Vote commitments can only be submitted during the active Commit Phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorCommitment.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already submitted a vote commitment for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment_hash = request.POST.get('commitment_hash')
    choice = request.POST.get('choice')
    salt = request.POST.get('salt')

    if choice and salt:
        if len(salt) < 16:
            messages.error(request, "Salt must meet minimum entropy requirements (at least 16 bytes/characters).")
            return redirect('dispute_detail', dispute_id=dispute.id)
        commitment_hash = JurorCommitment.compute_hash(choice, salt, request.user.id)

    if not commitment_hash or len(commitment_hash) != 64:
        messages.error(request, "A valid SHA-256 commitment hash (or vote choice and salt) is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    JurorCommitment.objects.create(
        dispute=dispute,
        juror=request.user,
        commitment_hash=commitment_hash
    )
    messages.success(request, "Vote commitment recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def reveal_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.phase != 'reveal' or dispute.status != 'open':
        messages.error(request, "Vote reveals are only accepted during the active Reveal Phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment = JurorCommitment.objects.filter(dispute=dispute, juror=request.user).first()
    if not commitment:
        messages.error(request, "No vote commitment found for this juror on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already revealed your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    salt = request.POST.get('salt')

    if not choice or not salt:
        messages.error(request, "Both vote choice and salt are required to reveal your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if len(salt) < 16:
        messages.error(request, "Salt must meet minimum entropy requirements (at least 16 bytes/characters).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    is_valid = commitment.verify_commitment(choice, salt)

    with transaction.atomic():
        vote = JurorVote.objects.create(
            dispute=dispute,
            juror=request.user,
            commitment=commitment,
            choice=choice,
            salt=salt,
            is_verified=is_valid
        )

        if not is_valid:
            RewardLedger.objects.create(
                user=request.user,
                task=dispute.task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Penalty for invalid vote reveal hash in dispute on task: '{dispute.task.title}'"
            )
            messages.error(request, "Cryptographic verification failed: Vote choice and salt do not match your stored commitment.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        messages.success(request, "Vote revealed and cryptographically verified successfully.")

        total_commitments = dispute.commitments.count()
        total_reveals = dispute.votes.filter(is_verified=True).count()
        if total_reveals >= total_commitments and total_commitments > 0:
            dispute.calculate_consensus()
            messages.success(request, "All vote commitments have been revealed. Dispute consensus calculated and resolved!")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def transition_phase(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.phase == 'commit':
        dispute.phase = 'reveal'
        dispute.save()
        messages.success(request, "Dispute transitioned from Commit Phase to Reveal Phase.")
    elif dispute.phase == 'reveal':
        dispute.calculate_consensus()
        messages.success(request, "Reveal phase closed and dispute consensus calculated.")
    else:
        messages.info(request, "Dispute is already concluded.")

    return redirect('dispute_detail', dispute_id=dispute.id)
