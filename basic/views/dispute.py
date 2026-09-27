from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JurorAssignment, DisputeVote
from ..services.juror import select_juror_pool
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_assigned_juror = JurorAssignment.objects.filter(dispute=dispute, juror=request.user).exists()
    is_participant = request.user in [task.posted_by, task.taken_by]

    if not is_participant and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    assignments = dispute.juror_assignments.select_related('juror').all()
    votes = dispute.votes.select_related('juror').all()

    poster_votes = votes.filter(vote='poster').count()
    taker_votes = votes.filter(vote='taker').count()
    total_votes = votes.count()

    has_voted = votes.filter(juror=request.user).exists()
    can_vote = is_assigned_juror and not has_voted and dispute.status == 'open'

    # Anonymize juror list for task participants during open dispute
    juror_list = []
    voted_juror_ids = set(votes.values_list('juror_id', flat=True))

    for idx, assignment in enumerate(assignments, start=1):
        juror_has_voted = assignment.juror_id in voted_juror_ids
        if is_participant and dispute.status == 'open':
            juror_list.append({
                'display_name': f"Juror #{idx}",
                'has_voted': juror_has_voted,
            })
        else:
            juror_list.append({
                'display_name': assignment.juror.username,
                'has_voted': juror_has_voted,
            })

    context = {
        'dispute': dispute,
        'task': task,
        'is_assigned_juror': is_assigned_juror,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'can_vote': can_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'juror_list': juror_list,
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

            # Select dynamic neutral juror panel
            select_juror_pool(dispute, panel_size=3)

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

    if not JurorAssignment.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reasoning = request.POST.get('reasoning', '')

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote=vote_choice,
            reasoning=reasoning
        )

        total_panel_size = dispute.juror_assignments.count()
        if total_panel_size == 0:
            total_panel_size = 3
        majority_threshold = (total_panel_size // 2) + 1

        poster_votes = dispute.votes.filter(vote='poster').count()
        taker_votes = dispute.votes.filter(vote='taker').count()

        if poster_votes >= majority_threshold or taker_votes >= majority_threshold:
            winning_side = 'poster' if poster_votes >= majority_threshold else 'taker'
            dispute.status = 'resolved'
            dispute.save()

            if winning_side == 'taker':
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded via dispute majority vote for task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                )
            else: # poster
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Task reward refunded via dispute majority vote for task: '{task.title}'"
                )
                task.status = 'cancelled'
                task.save()

                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Security deposit bond forfeited upon losing dispute for task: '{task.title}'"
                    )

            # Distribute micro-rewards to all voting jurors
            for vote_obj in dispute.votes.select_related('juror__userprofile').all():
                j_profile = vote_obj.juror.userprofile
                j_profile.rewards += 15
                j_profile.save()
                RewardLedger.objects.create(
                    user=vote_obj.juror,
                    task=task,
                    amount=15,
                    transaction_type='juror_reward',
                    description=f"Micro-reward for juror arbitration on task: '{task.title}'"
                )

            dispute_link = reverse('dispute_detail', args=[dispute.id])
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' has been resolved in favor of the task {winning_side}.",
                link=dispute_link
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' has been resolved in favor of the task {winning_side}.",
                    link=dispute_link
                )
            for assignment in dispute.juror_assignments.select_related('juror').all():
                Notification.objects.create(
                    recipient=assignment.juror,
                    message=f"Dispute for task '{task.title}' has reached majority consensus and is resolved.",
                    link=dispute_link
                )
            messages.success(request, f"Your vote has been cast. Dispute majority consensus reached; settled in favor of {winning_side}.")
        else:
            messages.success(request, "Your vote has been cast successfully.")

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
