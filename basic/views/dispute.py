from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, JurorVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    user_vote = JurorVote.objects.filter(dispute=dispute, voter=request.user).first()

    poster_votes_count = dispute.votes.filter(voted_for=task.posted_by).count()
    doer_votes_count = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
    total_votes = poster_votes_count + doer_votes_count

    context = {
        'dispute': dispute,
        'task': task,
        'is_party': is_party,
        'user_vote': user_vote,
        'poster_votes_count': poster_votes_count,
        'doer_votes_count': doer_votes_count,
        'total_votes': total_votes,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if (task.taken_by != request.user and task.posted_by != request.user) or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
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

            quorum_val = 3
            incentive_val = max(20, task.reward // 5)

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.quorum = quorum_val
                dispute.incentive_pool = incentive_val
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    quorum=quorum_val,
                    incentive_pool=incentive_val
                )

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='escrow_lock',
                description=f"Escrow lock for disputed task: '{task.title}'"
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

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user == task.posted_by or (task.taken_by and request.user == task.taken_by):
        messages.error(request, "Task posters and task doers are strictly prohibited from serving as jurors on their own task disputes.")
        return HttpResponseForbidden("Task posters and task doers are strictly prohibited from serving as jurors on their own task disputes.")

    if JurorVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "You must select a candidate to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for = get_object_or_404(User, id=voted_for_id)
    if voted_for != task.posted_by and voted_for != task.taken_by:
        messages.error(request, "Invalid vote target.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JurorVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for
        )
        messages.success(request, f"Your juror vote for {voted_for.username} has been recorded.")

        total_votes = dispute.votes.count()
        if total_votes >= dispute.quorum:
            resolve_dispute_logic(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)

def resolve_dispute_logic(dispute, forced_winner=None):
    if dispute.status != 'open':
        return

    task = dispute.task
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    doer_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    with transaction.atomic():
        if forced_winner:
            winning_user = forced_winner
        elif doer_votes > poster_votes:
            winning_user = task.taken_by
        else:
            winning_user = task.posted_by

        if task.taken_by and winning_user == task.taken_by:
            losing_user = task.posted_by
            task.status = 'completed'
            task.save()

            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += task.reward
            doer_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_payout',
                description=f"Dispute payout awarded for task: '{task.title}'"
            )
        else:
            winning_user = task.posted_by
            losing_user = task.taken_by
            task.status = 'cancelled'
            task.save()

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_refund',
                description=f"Dispute refund returned for task: '{task.title}'"
            )

        # Distribute juror rewards from incentive pool to majority voters
        majority_votes = dispute.votes.filter(voted_for=winning_user)
        num_winning_jurors = majority_votes.count()
        if num_winning_jurors > 0 and dispute.incentive_pool > 0:
            per_juror_reward = dispute.incentive_pool // num_winning_jurors
            if per_juror_reward > 0:
                for vote in majority_votes:
                    juror_profile = vote.voter.userprofile
                    juror_profile.rewards += per_juror_reward
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=vote.voter,
                        task=task,
                        amount=per_juror_reward,
                        transaction_type='juror_reward',
                        description=f"Juror reward for majority vote on dispute: '{task.title}'"
                    )

                    Notification.objects.create(
                        recipient=vote.voter,
                        message=f"You received {per_juror_reward} reward points for your majority juror vote on task '{task.title}'.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )

        # Handle security deposit bond
        if dispute.raised_by == winning_user:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for won dispute on task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=winning_user,
                reason_description=f"Security deposit bond forfeited for lost dispute on task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.save()

        Notification.objects.create(
            recipient=winning_user,
            message=f"Dispute for task '{task.title}' has been resolved in your favor.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if losing_user:
            Notification.objects.create(
                recipient=losing_user,
                message=f"Dispute for task '{task.title}' has been resolved in favor of {winning_user.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    forced_winner_id = request.POST.get('winner_id')
    forced_winner = None
    if forced_winner_id and (request.user.is_staff or request.user == task.posted_by or request.user == task.taken_by):
        forced_winner = get_object_or_404(User, id=forced_winner_id)

    resolve_dispute_logic(dispute, forced_winner=forced_winner)
    messages.success(request, f"Dispute resolved.")
    return redirect('dispute_detail', dispute_id=dispute.id)
