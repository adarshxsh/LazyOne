from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse


def resolve_dispute_by_consensus(dispute):
    """
    Resolves a dispute by community consensus vote tally.
    Must be called inside an atomic transaction.
    """
    votes = dispute.votes.all()
    posted_by_votes = votes.filter(vote_choice='posted_by').count()
    taken_by_votes = votes.filter(vote_choice='taken_by').count()

    if posted_by_votes == taken_by_votes:
        return False

    task = dispute.task
    if posted_by_votes > taken_by_votes:
        winner_choice = 'posted_by'
        winner_user = task.posted_by
    else:
        winner_choice = 'taken_by'
        winner_user = task.taken_by

    with transaction.atomic():
        if winner_choice == 'posted_by':
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded via community consensus for dispute on task: '{task.title}'"
            )
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
                    description=f"Awarded reward via community consensus for dispute on task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()

        if dispute.escrow_status == 'held':
            if dispute.raised_by == winner_user:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded following community consensus resolution on task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=winner_user,
                    reason_description=f"Security deposit bond forfeited following community consensus resolution on task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.save()

        # Award voter micro-rewards (10 points each)
        for vote in votes:
            voter_profile = getattr(vote.voter, 'userprofile', None)
            if voter_profile:
                voter_profile.rewards += 10
                voter_profile.save()

            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=10,
                transaction_type='voter_reward',
                description=f"Community dispute voting reward for task: '{task.title}'"
            )

        # Notify participants
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        participants = [task.posted_by]
        if task.taken_by and task.taken_by not in participants:
            participants.append(task.taken_by)

        for participant in participants:
            Notification.objects.create(
                recipient=participant,
                message=f"Dispute for task '{task.title}' has been resolved by community vote in favor of {winner_user.username}.",
                link=dispute_link
            )

    return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    has_voted = dispute.votes.filter(voter=request.user).exists()
    user_vote = dispute.votes.filter(voter=request.user).first()
    user_profile = getattr(request.user, 'userprofile', None)
    is_eligible_voter = bool(
        user_profile and (
            user_profile.rewards >= 50 or
            user_profile.is_phone_verified or
            user_profile.is_instagram_verified
        )
    )
    can_vote = (not is_participant) and (dispute.status == 'open') and (not has_voted) and is_eligible_voter

    votes_poster_count = dispute.votes.filter(vote_choice='posted_by').count()
    votes_taker_count = dispute.votes.filter(vote_choice='taken_by').count()
    total_votes = dispute.votes.count()
    votes_list = dispute.votes.select_related('voter').all().order_by('-created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'is_eligible_voter': is_eligible_voter,
        'can_vote': can_vote,
        'votes_poster_count': votes_poster_count,
        'votes_taker_count': votes_taker_count,
        'total_votes': total_votes,
        'votes_list': votes_list,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def public_disputes_list(request):
    disputes = Dispute.objects.filter(status='open').select_related('task', 'raised_by', 'task__posted_by', 'task__taken_by').order_by('-created_at')
    context = {
        'disputes': disputes,
    }
    return render(request, 'public_disputes.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants are strictly prohibited from voting on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    user_profile = getattr(request.user, 'userprofile', None)
    is_eligible = bool(
        user_profile and (
            user_profile.rewards >= 50 or
            user_profile.is_phone_verified or
            user_profile.is_instagram_verified
        )
    )
    if not is_eligible:
        messages.error(request, "You must have at least 50 reward points or a verified profile to vote on disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice')
    if vote_choice not in ['posted_by', 'taken_by']:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip()

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote_choice=vote_choice,
            reason=reason
        )
        messages.success(request, "Your vote has been recorded successfully.")

        if dispute.votes.count() >= 5:
            resolve_dispute_by_consensus(dispute)

    return redirect('dispute_detail', dispute_id=dispute.id)


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
