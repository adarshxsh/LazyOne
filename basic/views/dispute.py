from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    user_vote = dispute.votes.filter(voter=request.user).first()
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count()
    total_votes = dispute.votes.count()
    chat_messages = task.main_chat.messages.all() if task.main_chat else []

    context = {
        'dispute': dispute,
        'task': task,
        'is_party': is_party,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'chat_messages': chat_messages,
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
                user=request.user,
                task=task,
                amount=0,
                transaction_type='dispute_escrow_lock',
                description=f"Reward locked in dispute escrow for task: '{task.title}'"
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
def cast_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task posters and takers involved in a dispute cannot cast juror votes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.votes.filter(voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_choice = request.POST.get('voted_for')
    if voted_for_choice == 'poster':
        voted_for_user = task.posted_by
    elif voted_for_choice == 'taker':
        voted_for_user = task.taken_by
    else:
        voted_for_user_id = request.POST.get('voted_for_id')
        voted_for_user = get_object_or_404(User, id=voted_for_user_id) if voted_for_user_id else None

    if not voted_for_user or (voted_for_user != task.posted_by and voted_for_user != task.taken_by):
        messages.error(request, "Invalid voting selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        stake_amount = int(request.POST.get('stake_amount', 50))
        if stake_amount <= 0:
            messages.error(request, "Stake amount must be a positive integer.")
            return redirect('dispute_detail', dispute_id=dispute.id)
    except (ValueError, TypeError):
        messages.error(request, "Invalid stake amount.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voter_profile = request.user.userprofile
    if voter_profile.rewards < stake_amount:
        messages.error(request, f"You only have {voter_profile.rewards} points, which is not enough to stake {stake_amount} points.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        voter_profile.rewards -= stake_amount
        voter_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake_penalty',
            description=f"Juror stake for dispute on task: '{task.title}'"
        )

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for_user,
            stake_amount=stake_amount
        )

        messages.success(request, f"Your juror vote for {voted_for_user.username} has been recorded with a stake of {stake_amount} points.")
        check_and_settle_dispute(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)

def check_and_settle_dispute(dispute, threshold=3):
    total_votes = dispute.votes.count()
    if total_votes < threshold:
        return

    task = dispute.task
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count()

    if poster_votes == taker_votes:
        return

    winner = task.taken_by if taker_votes > poster_votes else task.posted_by

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.winner = winner
        dispute.save()

        if winner == task.taken_by:
            task.status = 'completed'
            task.save()

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_payout',
                description=f"Dispute payout for winning dispute on task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points transferred.",
                link=reverse('my_tasks')
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in favor of the taker.",
                link=reverse('my_tasks')
            )
        else:
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
                description=f"Dispute refund for winning dispute on task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points refunded.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of the poster.",
                    link=reverse('my_tasks')
                )

        winning_votes = list(dispute.votes.filter(voted_for=winner))
        losing_votes = list(dispute.votes.exclude(voted_for=winner))

        losing_stakes_pool = sum(v.stake_amount for v in losing_votes)
        total_winning_stakes = sum(v.stake_amount for v in winning_votes)

        for vote in winning_votes:
            juror_profile = vote.voter.userprofile
            if total_winning_stakes > 0 and losing_stakes_pool > 0:
                reward_share = int((vote.stake_amount / total_winning_stakes) * losing_stakes_pool)
            else:
                reward_share = 10

            total_payout = vote.stake_amount + reward_share
            juror_profile.rewards += total_payout
            juror_profile.save()

            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=total_payout,
                transaction_type='juror_reward',
                description=f"Juror reward for consensus vote on dispute for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=vote.voter,
                message=f"You earned {total_payout} points as a consensus juror reward for dispute on '{task.title}'.",
                link=reverse('rewards')
            )

        for vote in losing_votes:
            Notification.objects.create(
                recipient=vote.voter,
                message=f"Dispute for task '{task.title}' resolved. Your vote was not in majority consensus.",
                link=reverse('rewards')
            )

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status != 'open':
        messages.error(request, "Cannot withdraw a resolved dispute.")
        return redirect('my_tasks')

    task = dispute.task
    with transaction.atomic():
        for vote in dispute.votes.all():
            juror_profile = vote.voter.userprofile
            juror_profile.rewards += vote.stake_amount
            juror_profile.save()
            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=vote.stake_amount,
                transaction_type='juror_reward',
                description=f"Juror stake returned due to dispute withdrawal on task: '{task.title}'"
            )

        task.status = 'in_progress'
        task.save()
        dispute.delete()
        recipient_user = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient_user:
            Notification.objects.create(
                recipient=recipient_user,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')
