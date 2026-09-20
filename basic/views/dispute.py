from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth.models import User

from ..models import Dispute, Task, Notification, RewardLedger, JuryAssignment, DisputeVote


def assign_jurors_for_stage(dispute, stage='initial', panel_size=3):
    """
    Selects eligible users and creates JuryAssignment records for the given dispute and stage.
    Excludes task participants (posted_by, taken_by, raised_by), mutual friends,
    and for senior stage, excludes initial panel jurors.
    """
    task = dispute.task
    excluded_user_ids = set()
    if task.posted_by_id:
        excluded_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        excluded_user_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        excluded_user_ids.add(dispute.raised_by_id)

    # Exclude mutual friends of task participants
    for participant in [task.posted_by, task.taken_by]:
        if participant and hasattr(participant, 'userprofile'):
            friends = participant.userprofile.friends.all()
            for f in friends:
                excluded_user_ids.add(f.user_id)

    # For senior stage, exclude all jurors previously assigned in the initial stage
    if stage == 'senior':
        initial_juror_ids = JuryAssignment.objects.filter(
            dispute=dispute, stage='initial'
        ).values_list('juror_id', flat=True)
        excluded_user_ids.update(initial_juror_ids)

    already_assigned = JuryAssignment.objects.filter(
        dispute=dispute, stage=stage
    ).values_list('juror_id', flat=True)
    excluded_user_ids.update(already_assigned)

    candidates = User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids)

    assigned_list = []
    for candidate in candidates[:panel_size]:
        assignment, created = JuryAssignment.objects.get_or_create(
            dispute=dispute,
            juror=candidate,
            defaults={'stage': stage, 'staked_amount': 50}
        )
        if created:
            Notification.objects.create(
                recipient=candidate,
                message=f"You have been assigned as a juror for dispute on task: '{task.title}' ({stage} review).",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        assigned_list.append(assignment)
    return assigned_list


def finalize_dispute_resolution(dispute):
    task = dispute.task
    with transaction.atomic():
        winning_choice = dispute.winning_choice
        if not winning_choice:
            poster_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='poster').count()
            taker_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='taker').count()
            winning_choice = 'poster' if poster_votes >= taker_votes else 'taker'
            dispute.winning_choice = winning_choice

        # Execute task outcome
        if winning_choice == 'taker':
            task.status = 'completed'
            task.save()

            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task via dispute resolution: '{task.title}'"
                )

            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'")
            elif dispute.raised_by == task.posted_by:
                dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Security deposit bond forfeited for unsuccessful dispute on task: '{task.title}'")

        elif winning_choice == 'poster':
            task.status = 'cancelled'
            task.save()

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

            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'")
            elif dispute.raised_by == task.taken_by:
                dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Security deposit bond forfeited for unsuccessful dispute on task: '{task.title}'")

        # Appeal bond handling
        if dispute.appealed_by:
            appellant_choice = 'poster' if dispute.appealed_by == task.posted_by else 'taker'
            winning_party = task.posted_by if winning_choice == 'poster' else task.taken_by
            if appellant_choice == winning_choice:
                dispute.refund_appeal_deposit(reason_description=f"Appeal deposit bond refunded for successful appeal on task: '{task.title}'")
            else:
                dispute.forfeit_appeal_deposit(beneficiary=winning_party, reason_description=f"Appeal deposit bond forfeited for unsuccessful appeal on task: '{task.title}'")

        # Slashing bad-actor jurors
        slashed_pool = 0
        current_stage_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage)
        for vote in current_stage_votes:
            if vote.choice != winning_choice:
                juror_profile = vote.voter.userprofile
                assignment = JuryAssignment.objects.filter(dispute=dispute, juror=vote.voter, stage=dispute.stage).first()
                stake = assignment.staked_amount if assignment else 50

                actual_slash = min(juror_profile.rewards, stake)
                juror_profile.rewards -= actual_slash
                juror_profile.save()
                slashed_pool += actual_slash

                RewardLedger.objects.create(
                    user=vote.voter,
                    task=task,
                    amount=-actual_slash,
                    transaction_type='juror_slash',
                    description=f"Juror stake slashed for non-consensus vote on task: '{task.title}'"
                )
                Notification.objects.create(
                    recipient=vote.voter,
                    message=f"Your juror stake ({actual_slash} points) was slashed for voting against supermajority consensus on task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        # Distribute slashed pool to compliant jurors & winner
        compliant_votes = current_stage_votes.filter(choice=winning_choice)
        if compliant_votes.exists() and slashed_pool > 0:
            per_juror_bonus = slashed_pool // compliant_votes.count()
            residual = slashed_pool - (per_juror_bonus * compliant_votes.count())
            for vote in compliant_votes:
                juror_profile = vote.voter.userprofile
                juror_profile.rewards += per_juror_bonus
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.voter,
                    task=task,
                    amount=per_juror_bonus,
                    transaction_type='juror_reward',
                    description=f"Reward share from slashed bad-actor juror stakes on task: '{task.title}'"
                )
            if residual > 0:
                winning_user = task.posted_by if winning_choice == 'poster' else task.taken_by
                if winning_user:
                    winning_profile = winning_user.userprofile
                    winning_profile.rewards += residual
                    winning_profile.save()
                    RewardLedger.objects.create(
                        user=winning_user,
                        task=task,
                        amount=residual,
                        transaction_type='slashing_reward',
                        description=f"Residual slashing bonus from dispute resolution on task: '{task.title}'"
                    )

        dispute.status = 'resolved' if slashed_pool == 0 else 'slashed'
        dispute.save()

        for party in filter(None, [task.posted_by, task.taken_by]):
            Notification.objects.create(
                recipient=party,
                message=f"Dispute for task '{task.title}' has been resolved in favor of {winning_choice}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_assigned_juror = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).exists()

    if not is_participant and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    # Auto-finalize if 48h appeal window expired with no appeal
    if dispute.status == 'pending_consensus' and dispute.appeal_window_expires_at and timezone.now() >= dispute.appeal_window_expires_at:
        finalize_dispute_resolution(dispute)
        dispute.refresh_from_db()

    poster_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='poster').count()
    taker_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='taker').count()
    total_votes = poster_votes + taker_votes

    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user, stage=dispute.stage).first()
    can_vote = is_assigned_juror and user_vote is None and dispute.status in ['open', 'senior_review']

    time_remaining = None
    if dispute.appeal_window_expires_at and timezone.now() < dispute.appeal_window_expires_at:
        time_remaining = dispute.appeal_window_expires_at - timezone.now()

    assigned_jurors = JuryAssignment.objects.filter(dispute=dispute, stage=dispute.stage)

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'is_participant': is_participant,
        'is_appealable': dispute.is_appealable(),
        'appeal_bond_amount': task.deposit_bond_amount,
        'time_remaining': time_remaining,
        'assigned_jurors': assigned_jurors,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'pending_consensus', 'senior_review']:
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
                dispute.stage = 'initial'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.consensus_reached = False
                dispute.consensus_percentage = 0.0
                dispute.winning_choice = None
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    status='open',
                    stage='initial'
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

            # Assign initial peer jurors
            assign_jurors_for_stage(dispute, 'initial')

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
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    choice = request.POST.get('choice')

    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = JuryAssignment.objects.filter(
        dispute=dispute, juror=request.user, stage=dispute.stage
    ).first()

    if not assignment and not request.user.is_staff:
        messages.error(request, "You are not an assigned juror for this dispute stage.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user, stage=dispute.stage).exists():
        messages.error(request, "You have already cast your vote for this stage.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            stage=dispute.stage,
            choice=choice
        )

        poster_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='poster').count()
        taker_votes = DisputeVote.objects.filter(dispute=dispute, stage=dispute.stage, choice='taker').count()
        total_votes = poster_votes + taker_votes

        winning_votes = max(poster_votes, taker_votes)
        winning_option = 'poster' if poster_votes >= taker_votes else 'taker'
        consensus_pct = (winning_votes / total_votes) * 100.0 if total_votes > 0 else 0.0

        if consensus_pct >= 66.0:
            dispute.consensus_reached = True
            dispute.consensus_percentage = round(consensus_pct, 1)
            dispute.winning_choice = winning_option

            if dispute.stage == 'initial':
                dispute.status = 'pending_consensus'
                dispute.appeal_window_expires_at = timezone.now() + timedelta(hours=48)
                dispute.save()

                for party in filter(None, [task.posted_by, task.taken_by]):
                    Notification.objects.create(
                        recipient=party,
                        message=f"Dispute for '{task.title}' reached initial supermajority consensus ({dispute.consensus_percentage}%). 48-hour appeal window is now open.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                for assignment_item in dispute.jury_assignments.filter(stage='initial'):
                    Notification.objects.create(
                        recipient=assignment_item.juror,
                        message=f"Consensus vote tallied for task: '{task.title}' ({dispute.consensus_percentage}%).",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                messages.success(request, f"Vote recorded. 66%+ supermajority consensus quorum achieved ({dispute.consensus_percentage}%). Initial ruling pending consensus.")
            else:
                dispute.save()
                finalize_dispute_resolution(dispute)
                messages.success(request, f"Vote recorded. Senior review supermajority consensus achieved ({dispute.consensus_percentage}%). Dispute resolved.")
        else:
            dispute.consensus_reached = False
            dispute.consensus_percentage = round(consensus_pct, 1)
            dispute.save()
            messages.success(request, "Vote recorded successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def file_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can file a dispute appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appealable():
        messages.error(request, "This dispute is not currently open for appeal or the 48-hour appeal window has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_reason = request.POST.get('appeal_reason')
    if not appeal_reason:
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_bond_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile

    if user_profile.rewards < appeal_bond_amount:
        messages.error(request, f"Insufficient points for appeal bond. You need {appeal_bond_amount} points, but have {user_profile.rewards}.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= appeal_bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-appeal_bond_amount,
            transaction_type='appeal_deposit',
            description=f"Appeal deposit bond held for dispute on task: '{task.title}'"
        )

        dispute.status = 'senior_review'
        dispute.stage = 'senior'
        dispute.appealed_by = request.user
        dispute.appealed_at = timezone.now()
        dispute.appeal_reason = appeal_reason
        dispute.appeal_deposit_amount = appeal_bond_amount
        dispute.appeal_escrow_status = 'held'
        dispute.consensus_reached = False
        dispute.consensus_percentage = 0.0
        dispute.winning_choice = None
        dispute.save()

        # Assign senior jury panel excluding initial jurors
        assign_jurors_for_stage(dispute, 'senior')

        for party in filter(None, [task.posted_by, task.taken_by]):
            Notification.objects.create(
                recipient=party,
                message=f"{request.user.username} filed a formal appeal for dispute on task: '{task.title}'. Senior jury review initiated.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute appeal filed successfully. {appeal_bond_amount} points held as appeal bond.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def finalize_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by] and not request.user.is_staff:
        messages.error(request, "Unauthorized to finalize dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status == 'pending_consensus' and dispute.appeal_window_expires_at and timezone.now() >= dispute.appeal_window_expires_at:
        finalize_dispute_resolution(dispute)
        messages.success(request, "48-hour appeal window expired. Dispute resolution finalized.")
    elif dispute.consensus_reached and dispute.stage == 'senior':
        finalize_dispute_resolution(dispute)
        messages.success(request, "Senior review consensus finalized.")
    elif request.user.is_staff:
        finalize_dispute_resolution(dispute)
        messages.success(request, "Dispute resolution finalized by staff.")
    else:
        messages.error(request, "Dispute cannot be finalized yet. Appeal window is active.")

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
        if dispute.appealed_by:
            dispute.refund_appeal_deposit(
                reason_description=f"Appeal deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by if request.user == task.taken_by else task.taken_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
