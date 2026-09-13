from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote, DisputeAppeal

MIN_QUORUM = 3
SUPERMAJORITY_RATIO = 0.66
SLASH_AMOUNT = 50

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Check for auto-expiration of 48-hour appeal window if primary verdict exists
    if dispute.status != 'resolved' and dispute.primary_winner and not hasattr(dispute, 'appeal'):
        if timezone.now() > dispute.primary_verdict_at + timezone.timedelta(hours=48):
            finalize_dispute_resolution(dispute, dispute.primary_winner, source='auto_expire')
            dispute.refresh_from_db()

    # Tier 1 Voting Stats
    tier1_votes = dispute.votes.filter(tier=1)
    tier1_total = tier1_votes.count()
    tier1_posted = tier1_votes.filter(voted_for=task.posted_by).count()
    tier1_taken = tier1_votes.filter(voted_for=task.taken_by).count()
    tier1_consensus = round((max(tier1_posted, tier1_taken) / tier1_total * 100), 1) if tier1_total > 0 else 0.0
    tier1_quorum_met = tier1_total >= MIN_QUORUM
    tier1_consensus_met = (max(tier1_posted, tier1_taken) / tier1_total) >= SUPERMAJORITY_RATIO if tier1_total > 0 else False

    # Tier 2 Voting Stats
    tier2_votes = dispute.votes.filter(tier=2)
    tier2_total = tier2_votes.count()
    tier2_posted = tier2_votes.filter(voted_for=task.posted_by).count()
    tier2_taken = tier2_votes.filter(voted_for=task.taken_by).count()
    tier2_consensus = round((max(tier2_posted, tier2_taken) / tier2_total * 100), 1) if tier2_total > 0 else 0.0
    tier2_quorum_met = tier2_total >= MIN_QUORUM
    tier2_consensus_met = (max(tier2_posted, tier2_taken) / tier2_total) >= SUPERMAJORITY_RATIO if tier2_total > 0 else False

    # Check user voting eligibility
    user_voted_tier1 = dispute.votes.filter(voter=request.user, tier=1).exists()
    user_voted_tier2 = dispute.votes.filter(voter=request.user, tier=2).exists()
    is_party = (request.user == task.posted_by or request.user == task.taken_by)
    can_vote_tier1 = (not is_party) and (dispute.status == 'open') and (not user_voted_tier1)
    can_vote_tier2 = (not is_party) and (dispute.status in ['appealed', 'under_review']) and (not user_voted_tier2)

    # Appeal window
    appeal_active = dispute.is_appeal_window_active()
    appeal_expires_at = dispute.appeal_window_expires_at()
    time_remaining_seconds = 0
    if appeal_expires_at and appeal_expires_at > timezone.now():
        time_remaining_seconds = int((appeal_expires_at - timezone.now()).total_seconds())

    can_appeal = is_party and appeal_active and (not hasattr(dispute, 'appeal')) and (dispute.primary_winner is not None)

    # Slashing summary transactions
    slashing_transactions = RewardLedger.objects.filter(
        task=task,
        transaction_type__in=['juror_slashing', 'appellant_slashing', 'appeal_bond_escrow', 'appeal_bond_refund']
    ).order_by('-created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'tier1_total': tier1_total,
        'tier1_posted': tier1_posted,
        'tier1_taken': tier1_taken,
        'tier1_consensus': tier1_consensus,
        'tier1_quorum_met': tier1_quorum_met,
        'tier1_consensus_met': tier1_consensus_met,
        'tier2_total': tier2_total,
        'tier2_posted': tier2_posted,
        'tier2_taken': tier2_taken,
        'tier2_consensus': tier2_consensus,
        'tier2_quorum_met': tier2_quorum_met,
        'tier2_consensus_met': tier2_consensus_met,
        'user_voted_tier1': user_voted_tier1,
        'user_voted_tier2': user_voted_tier2,
        'can_vote_tier1': can_vote_tier1,
        'can_vote_tier2': can_vote_tier2,
        'is_party': is_party,
        'appeal_active': appeal_active,
        'appeal_expires_at': appeal_expires_at,
        'time_remaining_seconds': time_remaining_seconds,
        'can_appeal': can_appeal,
        'slashing_transactions': slashing_transactions,
        'min_quorum': MIN_QUORUM,
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
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
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
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Litigant parties in the dispute cannot vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status == 'resolved':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for_id')
    voted_for = get_object_or_404(User, id=voted_for_id)
    if voted_for not in [task.posted_by, task.taken_by]:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    tier = 1 if dispute.status == 'open' else 2

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user, tier=tier).exists():
        messages.error(request, f"You have already voted in Tier {tier} for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        voted_for=voted_for,
        tier=tier
    )

    if dispute.status == 'appealed':
        dispute.status = 'under_review'
        dispute.save()

    messages.success(request, f"Your vote for {voted_for.username} (Tier {tier}) has been recorded.")
    evaluate_dispute_consensus(dispute, tier)
    return redirect('dispute_detail', dispute_id=dispute.id)

def evaluate_dispute_consensus(dispute, tier):
    task = dispute.task
    votes = dispute.votes.filter(tier=tier)
    total_votes = votes.count()

    if total_votes < MIN_QUORUM:
        return

    posted_votes = votes.filter(voted_for=task.posted_by).count()
    taken_votes = votes.filter(voted_for=task.taken_by).count()

    if posted_votes >= taken_votes:
        leader = task.posted_by
        leader_votes = posted_votes
    else:
        leader = task.taken_by
        leader_votes = taken_votes

    consensus_ratio = leader_votes / total_votes

    if consensus_ratio >= SUPERMAJORITY_RATIO:
        if tier == 1 and dispute.status == 'open':
            dispute.primary_winner = leader
            dispute.primary_verdict_at = timezone.now()
            dispute.primary_consensus = round(consensus_ratio * 100, 1)
            dispute.save()

            msg = f"Primary dispute verdict issued in favor of {leader.username} with {dispute.primary_consensus}% consensus. 48-hour appeal window is now open."
            Notification.objects.create(recipient=task.posted_by, message=msg, link=reverse('dispute_detail', args=[dispute.id]))
            Notification.objects.create(recipient=task.taken_by, message=msg, link=reverse('dispute_detail', args=[dispute.id]))

        elif tier == 2 or dispute.status in ['appealed', 'under_review']:
            finalize_dispute_resolution(dispute, final_winner=leader, source='appeal')

@login_required(login_url='/login/')
@require_POST
def file_dispute_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task litigants can file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appeal_window_active():
        messages.error(request, "The 48-hour appeal window has expired or is not active.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if hasattr(dispute, 'appeal'):
        messages.error(request, "An appeal has already been filed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        bond_amount = int(request.POST.get('bond_amount', 100))
        if bond_amount <= 0:
            bond_amount = 100
    except (ValueError, TypeError):
        bond_amount = 100

    appellant_profile = request.user.userprofile
    if appellant_profile.rewards < bond_amount:
        messages.error(request, f"Insufficient reward balance. Required appeal bond deposit is {bond_amount} points.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip()

    with transaction.atomic():
        appellant_profile.rewards -= bond_amount
        appellant_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-bond_amount,
            transaction_type='appeal_bond_escrow',
            description=f"Appeal bond escrow deposit for dispute on '{task.title}'"
        )

        DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            bond_amount=bond_amount,
            reason=reason
        )

        dispute.status = 'appealed'
        dispute.save()

        opposing_user = task.posted_by if request.user == task.taken_by else task.taken_by
        Notification.objects.create(
            recipient=opposing_user,
            message=f"{request.user.username} has appealed the primary verdict for '{task.title}'. Escalated to Tier 2 Appeal Council.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        messages.success(request, f"Appeal filed successfully. Bond deposit of {bond_amount} points locked in escrow.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def staff_resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff administrators retain authorization to override dispute outcomes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    winner_id = request.POST.get('winner_id')
    winner = get_object_or_404(User, id=winner_id)
    if winner not in [task.posted_by, task.taken_by]:
        messages.error(request, "Invalid winner selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    finalize_dispute_resolution(dispute, final_winner=winner, source='staff')
    messages.success(request, f"Staff administrator override executed. Dispute resolved in favor of {winner.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

def finalize_dispute_resolution(dispute, final_winner, source='consensus'):
    with transaction.atomic():
        task = dispute.task
        dispute.final_winner = final_winner
        dispute.status = 'resolved'
        dispute.resolved_at = timezone.now()
        dispute.save()

        if hasattr(dispute, 'appeal'):
            appeal = dispute.appeal
            appeal.resolved_at = timezone.now()

            if final_winner == appeal.appellant:
                # Appeal Succeeded: Verdict Reversed!
                appeal.status = 'reversed'
                appeal.save()

                # Refund appeal bond deposit
                appellant_profile = appeal.appellant.userprofile
                appellant_profile.rewards += appeal.bond_amount
                appellant_profile.save()

                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=appeal.bond_amount,
                    transaction_type='appeal_bond_refund',
                    description=f"Refund of appeal bond escrow for successful appeal on '{task.title}'"
                )

                # Slash primary dissenting jurors (Tier 1) who voted against the final winning verdict
                primary_dissenting_votes = dispute.votes.filter(tier=1).exclude(voted_for=final_winner)
                for vote in primary_dissenting_votes:
                    juror_profile = vote.voter.userprofile
                    penalty = min(juror_profile.rewards, SLASH_AMOUNT)  # Guardrail: balance never drops below 0
                    if penalty > 0:
                        juror_profile.rewards -= penalty
                        juror_profile.save()

                        RewardLedger.objects.create(
                            user=vote.voter,
                            task=task,
                            amount=-penalty,
                            transaction_type='juror_slashing',
                            description=f"Juror slashing penalty for bad-actor vote against supermajority consensus on '{task.title}'"
                        )
                        Notification.objects.create(
                            recipient=vote.voter,
                            message=f"You were slashed {penalty} reward points for voting against final consensus in dispute on '{task.title}'.",
                            link=reverse('dispute_detail', args=[dispute.id])
                        )
            else:
                # Appeal Failed: Verdict Upheld
                appeal.status = 'upheld'
                appeal.save()

                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=-appeal.bond_amount,
                    transaction_type='appellant_slashing',
                    description=f"Forfeited appeal bond deposit for losing appeal on '{task.title}'"
                )

                # Slash Tier 2 dissenting jurors if any
                tier2_dissenting_votes = dispute.votes.filter(tier=2).exclude(voted_for=final_winner)
                for vote in tier2_dissenting_votes:
                    juror_profile = vote.voter.userprofile
                    penalty = min(juror_profile.rewards, SLASH_AMOUNT)
                    if penalty > 0:
                        juror_profile.rewards -= penalty
                        juror_profile.save()

                        RewardLedger.objects.create(
                            user=vote.voter,
                            task=task,
                            amount=-penalty,
                            transaction_type='juror_slashing',
                            description=f"Juror slashing penalty for incorrect appeal vote on '{task.title}'"
                        )

        # Award task reward to final winner
        if final_winner == task.taken_by:
            task.status = 'completed'
            doer_profile = task.taken_by.userprofile
            doer_profile.rewards += task.reward
            doer_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded via dispute resolution: '{task.title}'"
            )
        else:
            task.status = 'cancelled'
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for cancelled task via dispute resolution: '{task.title}'"
            )
        task.save()

        # Send resolution notifications
        res_msg = f"Dispute for task '{task.title}' has been finalized in favor of {final_winner.username}."
        Notification.objects.create(recipient=task.posted_by, message=res_msg, link=reverse('dispute_detail', args=[dispute.id]))
        Notification.objects.create(recipient=task.taken_by, message=res_msg, link=reverse('dispute_detail', args=[dispute.id]))
