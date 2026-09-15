from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

def resolve_dispute(dispute, winner=None):
    with transaction.atomic():
        task = dispute.task
        votes = dispute.votes.all()
        participating_jurors = [vote.voter for vote in votes]
        num_jurors = len(participating_jurors)

        if winner is None:
            taker_votes = votes.filter(Q(choice='taker') | Q(voted_for=task.taken_by)).count()
            poster_votes = votes.filter(Q(choice='poster') | Q(voted_for=task.posted_by)).count()
            if taker_votes > poster_votes:
                winner = task.taken_by
            else:
                winner = task.posted_by
        elif winner == 'taker':
            winner = task.taken_by
        elif winner == 'poster':
            winner = task.posted_by

        total_reward = task.reward
        if num_jurors > 0:
            total_juror_fee = int(total_reward * 0.10)
            per_juror_reward = total_juror_fee // num_jurors
            total_distributed_juror_rewards = per_juror_reward * num_jurors

            for juror in participating_jurors:
                juror_profile = juror.userprofile
                juror_profile.rewards += per_juror_reward
                juror_profile.save()

                RewardLedger.objects.create(
                    user=juror,
                    task=task,
                    amount=per_juror_reward,
                    transaction_type='juror_reward',
                    description=f"Juror reward for dispute on task: '{task.title}'"
                )
        else:
            total_distributed_juror_rewards = 0

        net_reward = total_reward - total_distributed_juror_rewards
        winner_profile = winner.userprofile
        winner_profile.rewards += net_reward
        winner_profile.save()

        if winner == task.taken_by:
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=net_reward,
                transaction_type='dispute_payout',
                description=f"Dispute payout for task: '{task.title}'"
            )
            task.status = 'completed'
        else:
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=net_reward,
                transaction_type='dispute_refund',
                description=f"Dispute refund for task: '{task.title}'"
            )
            task.status = 'cancelled'

        task.save()
        dispute.status = 'resolved'
        dispute.save()

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    user_vote = None
    if request.user.is_authenticated and hasattr(dispute, 'votes'):
        user_vote = dispute.votes.filter(voter=request.user).first()
    
    can_vote = (
        dispute.status == 'open' and
        request.user != task.posted_by and
        request.user != task.taken_by and
        user_vote is None
    )

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'can_vote': can_vote,
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
                amount=task.reward,
                transaction_type='dispute_hold',
                description=f"Escrow hold for disputed task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for = task.posted_by if choice == 'poster' else task.taken_by

    vote, created = DisputeVote.objects.get_or_create(
        dispute=dispute,
        voter=request.user,
        defaults={'choice': choice, 'voted_for': voted_for}
    )
    if not created:
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    messages.success(request, "Your vote has been submitted.")

    if dispute.votes.count() >= 5:
        resolve_dispute(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
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
