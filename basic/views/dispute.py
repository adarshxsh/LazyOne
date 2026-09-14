from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from ..models import Dispute, Task, Notification, DisputeVote, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    can_vote, vote_error_reason = dispute.can_vote(request.user)
    has_voted = DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists()
    user_vote = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()
    can_appeal, appeal_error_reason = dispute.can_appeal(request.user)
    
    votes_poster = dispute.votes.filter(vote='poster').count()
    votes_taker = dispute.votes.filter(vote='taker').count()
    total_votes = dispute.votes.count()

    context = {
        'dispute': dispute,
        'task': task,
        'can_vote': can_vote,
        'vote_error_reason': vote_error_reason,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'can_appeal': can_appeal,
        'appeal_error_reason': appeal_error_reason,
        'votes_poster': votes_poster,
        'votes_taker': votes_taker,
        'total_votes': total_votes,
        'appeal_bond_amount': task.reward,
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
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason, status='open')
        task.status = 'disputed'
        task.save()
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
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status in ['resolved_tier1', 'appealed', 'resolved_tier2']:
        messages.error(request, "Cannot withdraw a dispute that has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)
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
    dispute = get_object_or_404(Dispute, id=dispute_id)
    can_vote, reason = dispute.can_vote(request.user)
    if not can_vote:
        messages.error(request, reason)
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(dispute=dispute, juror=request.user, vote=vote_choice)
        if dispute.status == 'open':
            dispute.status = 'voting'
            dispute.save()

        total_votes = dispute.votes.count()
        poster_votes = dispute.votes.filter(vote='poster').count()
        taker_votes = dispute.votes.filter(vote='taker').count()
        max_votes = max(poster_votes, taker_votes)
        
        # Check Quorum (>= 3) and Supermajority (>= 66%)
        if total_votes >= 3 and (max_votes / total_votes) >= 0.66:
            if poster_votes > taker_votes:
                winning_side = 'poster'
                winner = dispute.task.posted_by
                loser = dispute.task.taken_by
            else:
                winning_side = 'taker'
                winner = dispute.task.taken_by
                loser = dispute.task.posted_by

            dispute.status = 'resolved_tier1'
            dispute.winner = winner
            dispute.winning_vote = winning_side
            dispute.resolved_tier1_at = timezone.now()
            dispute.save()

            task = dispute.task
            # Transfer/refund reward
            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()

            if winner == task.taken_by:
                RewardLedger.objects.create(
                    user=winner, task=task, amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Dispute Tier 1 victory award for task: '{task.title}'"
                )
            else:
                RewardLedger.objects.create(
                    user=winner, task=task, amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Dispute Tier 1 refund for task: '{task.title}'"
                )

            # Record litigant slashing record for loser
            RewardLedger.objects.create(
                user=loser, task=task, amount=0,
                transaction_type='litigant_slashing',
                description=f"Dispute Tier 1 ruling against litigant for task: '{task.title}'"
            )

            # Process juror rewards and slashing
            mediation_amount = 10
            for vote_obj in dispute.votes.all():
                juror = vote_obj.juror
                juror_profile = juror.userprofile
                if vote_obj.vote == winning_side:
                    juror_profile.rewards += mediation_amount
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=juror, task=task, amount=mediation_amount,
                        transaction_type='mediation_reward',
                        description=f"Mediation reward for dispute consensus on task: '{task.title}'"
                    )
                else:
                    slash_amount = min(10, max(0, juror_profile.rewards))
                    if slash_amount > 0:
                        juror_profile.rewards -= slash_amount
                        juror_profile.save()
                    RewardLedger.objects.create(
                        user=juror, task=task, amount=-slash_amount,
                        transaction_type='juror_slashing',
                        description=f"Juror slashing for voting against consensus on task: '{task.title}'"
                    )

            Notification.objects.create(
                recipient=winner,
                message=f"Tier 1 dispute resolved in your favor for task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            Notification.objects.create(
                recipient=loser,
                message=f"Tier 1 dispute resolved against you for task: '{task.title}'. You have 48 hours to file an appeal.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

            messages.success(request, "Vote recorded successfully. Tier 1 dispute resolution completed.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    messages.success(request, "Vote recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    can_appeal, reason = dispute.can_appeal(request.user)
    if not can_appeal:
        messages.error(request, reason)
        return redirect('dispute_detail', dispute_id=dispute.id)

    bond_amount = dispute.task.reward
    user_profile = request.user.userprofile
    if user_profile.rewards < bond_amount:
        messages.error(request, "You do not have enough reward points for the appeal bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user, task=dispute.task, amount=-bond_amount,
            transaction_type='appeal_bond_slash',
            description=f"Appeal bond deposited for dispute on task: '{dispute.task.title}'"
        )

        dispute.status = 'appealed'
        dispute.appellant = request.user
        dispute.appeal_bond_amount = bond_amount
        dispute.appealed_at = timezone.now()
        dispute.save()

        other_user = dispute.task.posted_by if request.user == dispute.task.taken_by else dispute.task.taken_by
        Notification.objects.create(
            recipient=other_user,
            message=f"{request.user.username} has appealed the Tier 1 ruling for task: '{dispute.task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Appeal submitted successfully. Escalated to Appellate Council.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not request.user.is_staff:
        messages.error(request, "You are not authorized to resolve appeals.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'appealed':
        messages.error(request, "This dispute is not currently under appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('decision')
    if decision not in ['uphold', 'overturn']:
        messages.error(request, "Invalid decision choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task

    with transaction.atomic():
        if decision == 'uphold':
            # Appellant loses posted appeal bond (already deducted and recorded as appeal_bond_slash)
            RewardLedger.objects.create(
                user=dispute.appellant, task=task, amount=0,
                transaction_type='litigant_slashing',
                description=f"Frivolous appeal lost for task: '{task.title}'"
            )
            dispute.status = 'resolved_tier2'
            dispute.tier2_decision = 'uphold'
            dispute.tier2_resolved_at = timezone.now()
            dispute.save()
            task.status = 'completed' if dispute.winner == task.taken_by else 'cancelled'
            task.save()
            messages.success(request, "Tier 2 appeal resolved: Original ruling upheld. Appeal bond forfeited.")
        else:
            # Overturn decision
            prev_winner = dispute.winner
            new_winner = dispute.appellant

            # Deduct task reward from previous winner
            prev_winner_profile = prev_winner.userprofile
            prev_winner_profile.rewards = max(0, prev_winner_profile.rewards - task.reward)
            prev_winner_profile.save()

            RewardLedger.objects.create(
                user=prev_winner, task=task, amount=-task.reward,
                transaction_type='litigant_slashing',
                description=f"Litigant slashing following overturned Tier 2 appeal for task: '{task.title}'"
            )

            # Refund appeal bond and award task reward to new winner
            appellant_profile = new_winner.userprofile
            appellant_profile.rewards += (dispute.appeal_bond_amount + task.reward)
            appellant_profile.save()

            RewardLedger.objects.create(
                user=new_winner, task=task, amount=dispute.appeal_bond_amount,
                transaction_type='task_cancellation',
                description=f"Appeal bond refunded for task: '{task.title}'"
            )
            RewardLedger.objects.create(
                user=new_winner, task=task, amount=task.reward,
                transaction_type='task_completion',
                description=f"Tier 2 victory award for task: '{task.title}'"
            )

            dispute.winner = new_winner
            dispute.winning_vote = 'taker' if new_winner == task.taken_by else 'poster'
            dispute.status = 'resolved_tier2'
            dispute.tier2_decision = 'overturn'
            dispute.tier2_resolved_at = timezone.now()
            dispute.save()

            task.status = 'completed' if new_winner == task.taken_by else 'cancelled'
            task.save()

            messages.success(request, "Tier 2 appeal resolved: Original ruling overturned. Appeal bond refunded.")

    return redirect('dispute_detail', dispute_id=dispute.id)
