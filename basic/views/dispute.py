import hashlib
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JuryAssignment, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    dispute.check_and_update_phase()
    task = dispute.task
    
    is_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    is_participant = request.user in [task.posted_by, task.taken_by]
    
    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    assignment = dispute.jury_assignments.filter(juror=request.user).first()
    user_vote = dispute.votes.filter(juror=request.user).first()

    # Obscure vote choices and intermediate tallies during commit phase
    if dispute.voting_phase == 'commit' or dispute.is_commit_phase():
        poster_votes = None
        taker_votes = None
        total_votes = None
    else:
        poster_votes = dispute.votes.filter(status='revealed', choice='poster').count()
        taker_votes = dispute.votes.filter(status='revealed', choice='taker').count()
        total_votes = poster_votes + taker_votes

    can_commit = is_juror and dispute.is_commit_phase()
    can_reveal = is_juror and dispute.is_reveal_phase() and user_vote is not None and user_vote.status == 'committed'

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'user_assignment': assignment,
        'user_vote': user_vote,
        'voting_phase': dispute.voting_phase,
        'is_commit_phase': dispute.is_commit_phase(),
        'is_reveal_phase': dispute.is_reveal_phase(),
        'can_commit': can_commit,
        'can_reveal': can_reveal,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def submit_vote_commitment(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    dispute.check_and_update_phase()

    assignment = dispute.jury_assignments.filter(juror=request.user).first()
    if not assignment and not request.user.is_staff:
        messages.error(request, "You are not assigned as a juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_commit_phase():
        messages.error(request, "Commit window is closed or dispute is not in commit phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment_hash = request.POST.get('commitment_hash', '').strip().lower()
    vote_choice = request.POST.get('vote', request.POST.get('choice', '')).strip()
    salt = request.POST.get('salt', '').strip()

    if not commitment_hash and vote_choice and salt:
        if len(salt) < 8:
            messages.error(request, "Salt string must be at least 8 characters long.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        commitment_hash = hashlib.sha256(f"{vote_choice}{salt}".encode('utf-8')).hexdigest().lower()

    if not commitment_hash or len(commitment_hash) != 64:
        messages.error(request, "Invalid commitment hash. Must be a 64-character SHA-256 hash.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        vote, created = DisputeVote.objects.get_or_create(
            dispute=dispute,
            juror=request.user,
            defaults={'commitment_hash': commitment_hash, 'status': 'committed'}
        )
        if not created:
            vote.commitment_hash = commitment_hash
            vote.status = 'committed'
            vote.save()

        if assignment:
            assignment.status = 'committed'
            assignment.save()

    messages.success(request, "Vote commitment submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_vote_reveal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    dispute.check_and_update_phase()

    assignment = dispute.jury_assignments.filter(juror=request.user).first()
    if not assignment and not request.user.is_staff:
        messages.error(request, "You are not assigned as a juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_reveal_phase():
        messages.error(request, "Reveal window is closed or dispute is not in reveal phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote = dispute.votes.filter(juror=request.user).first()
    if not vote or not vote.commitment_hash or vote.status not in ['committed', 'revealed']:
        messages.error(request, "No active vote commitment found for reveal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice', request.POST.get('vote', '')).strip().lower()
    salt = request.POST.get('salt', '').strip()

    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice. Must be 'poster' or 'taker'.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if len(salt) < 8:
        messages.error(request, "Salt string must be at least 8 characters long.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    computed_hash = hashlib.sha256(f"{choice}{salt}".encode('utf-8')).hexdigest().lower()
    if computed_hash != vote.commitment_hash.lower():
        messages.error(request, "Verification failed! Plaintext vote choice and salt do not match the submitted commitment hash.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        vote.choice = choice
        vote.salt = salt
        vote.status = 'revealed'
        vote.revealed_at = timezone.now()
        vote.save()

        if assignment:
            assignment.status = 'revealed'
            assignment.save()

        messages.success(request, "Vote revealed and verified successfully!")

        total_assigned = dispute.jury_assignments.count()
        revealed_count = dispute.votes.filter(status='revealed').count()
        if total_assigned > 0 and revealed_count >= total_assigned:
            finalize_dispute_settlement(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)

def finalize_dispute_settlement(dispute):
    if dispute.status == 'resolved':
        return

    with transaction.atomic():
        # Mark unrevealed commitments as expired
        unrevealed_votes = dispute.votes.filter(status='committed')
        for vote in unrevealed_votes:
            vote.status = 'expired'
            vote.save()

        unrevealed_assignments = dispute.jury_assignments.filter(status__in=['assigned', 'committed'])
        for assignment in unrevealed_assignments:
            assignment.status = 'expired'
            assignment.save()

        revealed_votes = dispute.votes.filter(status='revealed')
        poster_votes = revealed_votes.filter(choice='poster').count()
        taker_votes = revealed_votes.filter(choice='taker').count()

        if taker_votes > poster_votes:
            winner = 'taker'
        elif poster_votes > taker_votes:
            winner = 'poster'
        else:
            winner = 'poster'

        task = dispute.task
        if winner == 'taker':
            task.status = 'completed'
            task.save()
            dispute.refund_deposit(reason_description=f"Dispute resolved in favor of taker for task: '{task.title}'")
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward for dispute resolution victory on task: '{task.title}'"
                )
        else:
            task.status = 'cancelled'
            task.save()
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(reason_description=f"Dispute resolved in favor of poster for task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Dispute deposit forfeited to poster for task: '{task.title}'")
            
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded after dispute resolution for task: '{task.title}'"
            )

        # Distribute juror reward allocations to revealed jurors
        if revealed_votes.exists():
            incentive_pool = 100
            per_juror_reward = max(10, incentive_pool // revealed_votes.count())
            for r_vote in revealed_votes:
                j_profile = r_vote.juror.userprofile
                j_profile.rewards += per_juror_reward
                j_profile.save()
                RewardLedger.objects.create(
                    user=r_vote.juror,
                    task=task,
                    amount=per_juror_reward,
                    transaction_type='juror_reward',
                    description=f"Juror reward for participating in dispute voting for task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.voting_phase = 'closed'
        dispute.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' has been resolved.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' has been resolved.",
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
