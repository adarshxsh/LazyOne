from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    if not is_participant and not request.user.is_staff and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    if request.method == 'POST':
        if is_participant:
            messages.error(request, "Task participants cannot vote on their own dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if dispute.status != 'open':
            messages.error(request, "This dispute is not open for voting.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
            messages.error(request, "You have already voted on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        voted_for_id = request.POST.get('voted_for')
        if not voted_for_id or (int(voted_for_id) != task.posted_by.id and (not task.taken_by or int(voted_for_id) != task.taken_by.id)):
            messages.error(request, "Invalid vote recipient selection.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        voted_for_user = get_object_or_404(User, id=voted_for_id)

        with transaction.atomic():
            DisputeVote.objects.create(
                dispute=dispute,
                voter=request.user,
                voted_for=voted_for_user
            )

            total_votes = dispute.votes.count()
            if total_votes >= 5:
                poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
                taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

                if poster_votes > taker_votes:
                    winner = task.posted_by
                else:
                    winner = task.taken_by

                if winner == task.posted_by:
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refund for community dispute resolution on task: '{task.title}'"
                    )

                    if dispute.raised_by == task.posted_by:
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
                    else:
                        dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited for lost dispute on task: '{task.title}'")

                    task.status = 'cancelled'
                    task.save()
                    dispute.status = 'resolved'
                    dispute.save()
                else:
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded reward for community dispute resolution on task: '{task.title}'"
                        )

                        if dispute.raised_by == task.taken_by:
                            dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
                        else:
                            dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited for lost dispute on task: '{task.title}'")

                    task.status = 'completed'
                    task.save()
                    dispute.status = 'resolved'
                    dispute.save()

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' has been resolved by community consensus in favor of {winner.username}.",
                    link=dispute_link
                )
                if task.taken_by and task.taken_by != task.posted_by:
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Dispute for task '{task.title}' has been resolved by community consensus in favor of {winner.username}.",
                        link=dispute_link
                    )

                messages.success(request, f"Your vote has been cast. Consensus threshold reached and dispute resolved in favor of {winner.username}!")
            else:
                messages.success(request, f"Your vote for {voted_for_user.username} has been recorded.")

        return redirect('dispute_detail', dispute_id=dispute.id)

    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
    user_voted = user_vote is not None
    total_votes = dispute.votes.count()
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
    can_vote = (not is_participant) and (not user_voted) and (dispute.status == 'open')

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'user_voted': user_voted,
        'user_vote': user_vote,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'can_vote': can_vote,
        'quorum_threshold': 5,
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
