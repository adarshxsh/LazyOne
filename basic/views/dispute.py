from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JurorAssignment
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..utils import select_and_assign_jurors

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = dispute.juror_assignments.filter(juror=request.user).exists()
    
    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_assignment = dispute.juror_assignments.filter(juror=request.user).first()
    can_vote = (user_assignment is not None and user_assignment.status == 'assigned' and dispute.status == 'open')

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'user_assignment': user_assignment,
        'can_vote': can_vote,
        'juror_assignments': dispute.juror_assignments.all(),
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'pending_jurors']:
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

            # Automated Juror Selection within the same atomic transaction
            juror_assignments = select_and_assign_jurors(dispute, stake_amount=50, required_count=3)

        if juror_assignments:
            messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Jury panel assigned.")
        else:
            messages.warning(request, f"Dispute raised. {deposit_amount} points held as deposit bond. Pending sufficient eligible jurors for panel creation.")

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
        dispute.release_all_juror_stakes()
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
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    assignment = get_object_or_404(JurorAssignment, dispute=dispute, juror=request.user)

    if assignment.status != 'assigned':
        messages.error(request, "You have already voted or are no longer eligible to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        assignment.status = 'voted'
        assignment.vote_choice = vote_choice
        assignment.voted_at = timezone.now()
        assignment.save()

        messages.success(request, f"Your vote for '{vote_choice}' has been recorded.")

        # Check if all assigned jurors have voted
        remaining_unvoted = dispute.juror_assignments.filter(status='assigned').count()
        if remaining_unvoted == 0:
            poster_votes = dispute.juror_assignments.filter(status='voted', vote_choice='poster').count()
            taker_votes = dispute.juror_assignments.filter(status='voted', vote_choice='taker').count()

            winning_choice = 'poster' if poster_votes >= taker_votes else 'taker'
            
            dispute.status = 'resolved'
            dispute.save()

            task = dispute.task
            if winning_choice == 'poster':
                task.status = 'cancelled'
                task.save()
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Dispute resolved in favor of poster for task: '{task.title}'"
                )
            else:
                task.status = 'completed'
                task.save()
                dispute.refund_deposit(
                    reason_description=f"Dispute resolved in favor of taker for task: '{task.title}'"
                )
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward earned for task: '{task.title}' post dispute resolution"
                )

            dispute.resolve_jury_outcomes(winning_choice)

    return redirect('dispute_detail', dispute_id=dispute.id)

