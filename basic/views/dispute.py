from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, DisputeJuror
from ..services.juror_selection import ORMFilteredJurorSelectionService
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = dispute.jurors.filter(user=request.user).exists()
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_juror = dispute.jurors.filter(user=request.user).first()
    posted_by_votes = dispute.jurors.filter(vote='posted_by').count()
    taken_by_votes = dispute.jurors.filter(vote='taken_by').count()
    pending_votes = dispute.jurors.filter(vote='pending').count()
    total_jurors = dispute.jurors.count()

    context = {
        'dispute': dispute,
        'task': task,
        'jurors': dispute.jurors.all(),
        'is_juror': is_juror,
        'user_juror': user_juror,
        'posted_by_votes': posted_by_votes,
        'taken_by_votes': taken_by_votes,
        'pending_votes': pending_votes,
        'total_jurors': total_jurors,
    }
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
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.verdict = 'pending'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    verdict='pending'
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

            # Execute ORM candidate selection upon dispute creation
            ORMFilteredJurorSelectionService.select_and_assign_jurors(dispute, k=3)

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
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror = dispute.jurors.filter(user=request.user).first()
    if not juror:
        messages.error(request, "You are not authorized to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if juror.vote != 'pending':
        messages.info(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    raw_vote = request.POST.get('vote') or request.POST.get('vote_choice')
    notes = request.POST.get('notes', '')

    if raw_vote in ['posted_by', 'poster']:
        vote_val = 'posted_by'
    elif raw_vote in ['taken_by', 'taker', 'doer']:
        vote_val = 'taken_by'
    else:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror.vote = vote_val
        juror.voted_at = timezone.now()
        juror.notes = notes
        juror.save()

        posted_votes = dispute.jurors.filter(vote='posted_by').count()
        taken_votes = dispute.jurors.filter(vote='taken_by').count()
        total_assigned = dispute.jurors.count()
        majority_needed = (total_assigned // 2) + 1 if total_assigned > 0 else 1

        if posted_votes >= majority_needed:
            dispute.verdict = 'posted_by'
            dispute.juror_pool_status = 'completed'
            dispute.status = 'resolved'

            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded for dispute won on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Security deposit bond forfeited to poster for dispute won on task: '{task.title}'")

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund of reward points for dispute won on task: '{task.title}'"
            )

            task.status = 'cancelled'
            task.save()
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in your favor by peer jury.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of task poster by peer jury.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, "Your vote was recorded. The dispute has been resolved in favor of the task poster.")

        elif taken_votes >= majority_needed:
            dispute.verdict = 'taken_by'
            dispute.juror_pool_status = 'completed'
            dispute.status = 'resolved'

            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded for dispute won on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Security deposit bond forfeited to worker for dispute won on task: '{task.title}'")

            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward points for dispute won on task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in favor of task assignee by peer jury.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor by peer jury.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, "Your vote was recorded. The dispute has been resolved in favor of the task assignee.")

        else:
            messages.success(request, "Your vote has been recorded.")

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
