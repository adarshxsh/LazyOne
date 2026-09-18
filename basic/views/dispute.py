from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..dispute_logic import is_eligible_juror, evaluate_dispute_state, file_appeal

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    evaluate_dispute_state(dispute)
    dispute.refresh_from_db()

    eligible = is_eligible_juror(request.user, dispute)

    active_stage = None
    if dispute.status in ['open', 'voting_primary']:
        active_stage = 'primary'
    elif dispute.status == 'appeal_pending':
        active_stage = 'appeal'

    user_has_voted = False
    if active_stage and request.user.is_authenticated:
        user_has_voted = dispute.votes.filter(juror=request.user, stage=active_stage).exists()

    appeal_bond_amount = max(1, int(0.20 * task.reward))

    primary_votes = dispute.votes.filter(stage='primary')
    appeal_votes = dispute.votes.filter(stage='appeal')

    context = {
        'dispute': dispute,
        'task': task,
        'is_eligible_juror': eligible,
        'active_stage': active_stage,
        'user_has_voted': user_has_voted,
        'appeal_bond_amount': appeal_bond_amount,
        'primary_poster_votes': primary_votes.filter(vote='poster_wins').count(),
        'primary_taker_votes': primary_votes.filter(vote='taker_wins').count(),
        'appeal_poster_votes': appeal_votes.filter(vote='poster_wins').count(),
        'appeal_taker_votes': appeal_votes.filter(vote='taker_wins').count(),
        'is_litigant': request.user in [task.posted_by, task.taken_by],
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'voting_primary', 'appeal_window', 'appeal_pending']:
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

        ends_at = timezone.now() + timedelta(hours=48)
        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'voting_primary'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.primary_voting_ends_at = ends_at
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='voting_primary',
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    primary_voting_ends_at=ends_at
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
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Primary jury voting is now active.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status not in ['open', 'voting_primary']:
        messages.error(request, "Dispute cannot be withdrawn once primary voting has ended or appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

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

@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    evaluate_dispute_state(dispute)
    dispute.refresh_from_db()

    if dispute.status not in ['open', 'voting_primary', 'appeal_pending']:
        messages.error(request, "Voting is not currently active for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    active_stage = 'primary' if dispute.status in ['open', 'voting_primary'] else 'appeal'

    if not is_eligible_juror(request.user, dispute):
        messages.error(request, "You are not eligible to vote as a juror on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user, stage=active_stage).exists():
        messages.error(request, "You have already cast a vote in this stage.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster_wins', 'taker_wins']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        juror=request.user,
        vote=vote_choice,
        stage=active_stage
    )
    messages.success(request, "Your vote has been submitted successfully.")

    evaluate_dispute_state(dispute)
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def file_dispute_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    evaluate_dispute_state(dispute)
    dispute.refresh_from_db()

    success, message = file_appeal(dispute, request.user)
    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)

    return redirect('dispute_detail', dispute_id=dispute.id)
