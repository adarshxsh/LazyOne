from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()

    poster_votes_count = dispute.votes.filter(voted_for=task.posted_by).count()
    doer_votes_count = dispute.votes.filter(voted_for=task.taken_by).count()
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
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        with transaction.atomic():
            dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
            task.status = 'disputed'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=0,
                transaction_type='dispute_escrow_lock',
                description=f"Escrow locked for disputed task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, "Dispute raised successfully. Escrow funds locked.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and task doers are strictly prohibited from serving as jurors on their own task disputes.")
        return HttpResponseForbidden("Task posters and task doers are strictly prohibited from serving as jurors on their own task disputes.")

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
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

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        voted_for=voted_for
    )
    messages.success(request, f"Your vote for {voted_for.username} has been recorded.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    with transaction.atomic():
        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        doer_votes = dispute.votes.filter(voted_for=task.taken_by).count()

        forced_winner_id = request.POST.get('winner_id')
        if forced_winner_id and (request.user.is_staff or request.user == task.posted_by or request.user == task.taken_by):
            winning_user = get_object_or_404(User, id=forced_winner_id)
        elif doer_votes > poster_votes:
            winning_user = task.taken_by
        elif poster_votes > doer_votes:
            winning_user = task.posted_by
        else:
            winning_user = task.posted_by

        slash_amount = max(20, task.reward // 5)

        if winning_user == task.taken_by:
            losing_user = task.posted_by
            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += task.reward
            doer_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_payout_doer',
                description=f"Dispute resolution payout for task: '{task.title}'"
            )

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards -= slash_amount
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=-slash_amount,
                transaction_type='dispute_slash',
                description=f"Dispute slash penalty for lost dispute on task: '{task.title}'"
            )
            task.status = 'completed'

        else:
            losing_user = task.taken_by
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_refund_poster',
                description=f"Dispute refund for task: '{task.title}'"
            )

            doer_profile = task.taken_by.userprofile
            doer_profile.rewards -= slash_amount
            doer_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=-slash_amount,
                transaction_type='dispute_slash',
                description=f"Dispute slash penalty for lost dispute on task: '{task.title}'"
            )
            task.status = 'cancelled'

        majority_votes = dispute.votes.filter(voted_for=winning_user)
        num_winning_jurors = majority_votes.count()
        if num_winning_jurors > 0 and slash_amount > 0:
            per_juror_reward = slash_amount // num_winning_jurors
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
                        message=f"You earned {per_juror_reward} points for voting with the majority in dispute '{task.title}'.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )

        dispute.status = 'resolved'
        dispute.save()
        task.save()

        Notification.objects.create(
            recipient=winning_user,
            message=f"Dispute for task '{task.title}' has been resolved in your favor.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=losing_user,
            message=f"Dispute for task '{task.title}' has been resolved in favor of {winning_user.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute resolved in favor of {winning_user.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)
