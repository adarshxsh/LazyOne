import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, JuryAssignment, DisputeVote, RewardLedger, Conversation, Message

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_participant = request.user in [task.posted_by, task.taken_by]
    is_staff = request.user.is_staff
    user_assignment = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).first()
    is_juror = user_assignment is not None

    if not (is_participant or is_staff or is_juror):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()
    can_vote = (dispute.status == 'open') and is_juror and (user_vote is None)

    # Task communication history
    chat_messages = []
    conversation = getattr(task, 'conversation', None)
    if conversation:
        chat_messages = conversation.messages.order_by('timestamp')

    stake_amount = getattr(settings, 'JURY_STAKE_AMOUNT', 50)

    # Resolution outcome details if resolved
    winner_username = None
    if dispute.status == 'resolved':
        if task.status == 'completed' and task.taken_by:
            winner_username = task.taken_by.username
        elif task.status == 'cancelled' and task.posted_by:
            winner_username = task.posted_by.username

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'user_assignment': user_assignment,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'chat_messages': chat_messages,
        'stake_amount': stake_amount,
        'winner_username': winner_username,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task in progress that you are involved in.")
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

        stake_amount = getattr(settings, 'JURY_STAKE_AMOUNT', 50)
        panel_size = getattr(settings, 'JURY_PANEL_SIZE', 3)

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

            # Select neutral community jurors with sufficient points
            excluded_user_ids = [task.posted_by.id]
            if task.taken_by:
                excluded_user_ids.append(task.taken_by.id)

            eligible_jurors = list(
                User.objects.exclude(id__in=excluded_user_ids)
                .filter(userprofile__rewards__gte=stake_amount)
            )

            num_to_select = min(len(eligible_jurors), panel_size)
            if num_to_select > 0 and num_to_select % 2 == 0:
                num_to_select -= 1

            selected_jurors = random.sample(eligible_jurors, num_to_select) if num_to_select > 0 else []

            for juror in selected_jurors:
                JuryAssignment.objects.create(
                    dispute=dispute,
                    juror=juror,
                    stake_amount=stake_amount
                )
                Notification.objects.create(
                    recipient=juror,
                    message=f"You have been selected as a community juror for dispute on task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            # Notify counterparty
            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).first()
    if not assignment:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    stake_amount = assignment.stake_amount

    with transaction.atomic():
        juror_profile = request.user.userprofile
        if juror_profile.rewards < stake_amount:
            messages.error(request, f"You require at least {stake_amount} reward points to stake and vote.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        juror_profile.rewards -= stake_amount
        juror_profile.save()

        assignment.is_staked = True
        assignment.staked_at = timezone.now()
        assignment.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Staked {stake_amount} points for dispute vote on task: '{task.title}'"
        )

        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            choice=choice
        )

        # Check for supermajority consensus or full panel completion
        total_assignments = dispute.assignments.count()
        votes = list(dispute.votes.all())
        poster_votes = sum(1 for v in votes if v.choice == 'poster')
        taker_votes = sum(1 for v in votes if v.choice == 'taker')
        
        # Supermajority threshold (majority of total panel assignments)
        votes_needed = (total_assignments // 2) + 1 if total_assignments > 0 else 1

        winner = None
        if poster_votes >= votes_needed:
            winner = 'poster'
        elif taker_votes >= votes_needed:
            winner = 'taker'
        elif total_assignments > 0 and len(votes) == total_assignments:
            if poster_votes > taker_votes:
                winner = 'poster'
            elif taker_votes > poster_votes:
                winner = 'taker'
            else:
                winner = 'poster'

        if winner:
            _settle_dispute_consensus(dispute, winner)

    messages.success(request, "Your secret vote and point stake have been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


def _settle_dispute_consensus(dispute, winner_choice):
    """
    Settle task escrow, return stakes + bonus to majority jurors,
    forfeit stakes of minority/non-voting jurors, and settle dispute deposit bond.
    Must be executed inside transaction.atomic().
    """
    task = dispute.task
    dispute.status = 'resolved'
    dispute.save()

    # Refund or forfeit dispute deposit bond
    if dispute.deposit_amount > 0 and dispute.escrow_status == 'held':
        winner_user = task.taken_by if winner_choice == 'taker' else task.posted_by
        if dispute.raised_by == winner_user:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
            )
        else:
            dispute.forfeit_deposit(
                beneficiary=winner_user,
                reason_description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
            )

    stake_amount = getattr(settings, 'JURY_STAKE_AMOUNT', 50)
    bonus_reward = getattr(settings, 'JURY_BONUS_REWARD', 10)

    # 1. Escrow Settlement
    if winner_choice == 'taker' and task.taken_by:
        task.status = 'completed'
        task.save()

        taker_profile = task.taken_by.userprofile
        taker_profile.rewards += task.reward
        taker_profile.save()

        RewardLedger.objects.create(
            user=task.taken_by,
            task=task,
            amount=task.reward,
            transaction_type='dispute_settlement_taker',
            description=f"Dispute resolved in favor of taker for task: '{task.title}'"
        )
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute resolved in your favor for task: '{task.title}'! {task.reward} points transferred.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute on task '{task.title}' was resolved by community jury in favor of the task taker.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
    else: # Winner is poster
        task.status = 'cancelled'
        task.save()

        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=task.reward,
            transaction_type='dispute_settlement_poster',
            description=f"Dispute resolved in favor of poster for task: '{task.title}'"
        )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute resolved in your favor for task: '{task.title}'! {task.reward} points refunded.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute on task '{task.title}' was resolved by community jury in favor of the task poster.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    # 2. Juror Incentive & Penalty Distribution
    majority_votes = dispute.votes.filter(choice=winner_choice)
    majority_count = majority_votes.count()

    # Calculate total slashed stakes from minority voters and non-voters who staked
    slashed_assignments = dispute.assignments.exclude(
        juror__in=[v.juror for v in majority_votes]
    ).filter(is_staked=True)
    total_slashed = sum(a.stake_amount for a in slashed_assignments)
    
    extra_bonus = (total_slashed // majority_count) if majority_count > 0 else 0
    total_payout = stake_amount + bonus_reward + extra_bonus

    for vote in majority_votes:
        voter_profile = vote.juror.userprofile
        voter_profile.rewards += total_payout
        voter_profile.save()

        RewardLedger.objects.create(
            user=vote.juror,
            task=task,
            amount=total_payout,
            transaction_type='juror_reward',
            description=f"Jury stake return and bonus reward for dispute consensus on task: '{task.title}'"
        )
        Notification.objects.create(
            recipient=vote.juror,
            message=f"Supermajority consensus reached on task '{task.title}'. Your stake was returned with a {total_payout - stake_amount} point bonus!",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    # Log penalties for minority voters
    minority_votes = dispute.votes.exclude(choice=winner_choice)
    for vote in minority_votes:
        RewardLedger.objects.create(
            user=vote.juror,
            task=task,
            amount=0,
            transaction_type='dispute_penalty',
            description=f"Jury stake forfeited for minority vote on task: '{task.title}'"
        )
        Notification.objects.create(
            recipient=vote.juror,
            message=f"Dispute consensus reached on task '{task.title}'. Your vote was in the minority and your stake was forfeited.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    if dispute.status != 'open':
        messages.error(request, "Cannot withdraw a resolved dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        # Refund deposit bond
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        # Refund stakes to any jurors who staked prior to withdrawal
        for assignment in dispute.assignments.filter(is_staked=True):
            juror_profile = assignment.juror.userprofile
            juror_profile.rewards += assignment.stake_amount
            juror_profile.save()
            RewardLedger.objects.create(
                user=assignment.juror,
                task=task,
                amount=assignment.stake_amount,
                transaction_type='juror_reward',
                description=f"Refund of staked points due to dispute withdrawal on task: '{task.title}'"
            )

        task.status = 'in_progress'
        task.save()

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

