import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.models import User

from ..models import Dispute, Task, Notification, RewardLedger, JuryAssignment, DisputeVote, Friendship, UserProfile


def assign_jury_pool(dispute, tier=1, pool_size=3):
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    # Exclude participants
    excluded_user_ids = {poster.id}
    if taker:
        excluded_user_ids.add(taker.id)

    # Exclude mutual friends of poster and taker
    if hasattr(poster, 'userprofile'):
        excluded_user_ids.update(poster.userprofile.friends.values_list('user_id', flat=True))
    if taker and hasattr(taker, 'userprofile'):
        excluded_user_ids.update(taker.userprofile.friends.values_list('user_id', flat=True))

    friendship_user_ids = set(
        Friendship.objects.filter(
            from_user__user__in=[u for u in [poster, taker] if u]
        ).values_list('to_user__user_id', flat=True)
    ) | set(
        Friendship.objects.filter(
            to_user__user__in=[u for u in [poster, taker] if u]
        ).values_list('from_user__user_id', flat=True)
    )
    excluded_user_ids.update(friendship_user_ids)

    # If Tier 2 (Appeal Jury), exclude Tier 1 assigned jurors
    if tier == 2:
        tier1_juror_ids = JuryAssignment.objects.filter(
            dispute=dispute, tier=1
        ).values_list('juror_id', flat=True)
        excluded_user_ids.update(tier1_juror_ids)

    # Select eligible users
    eligible_users = list(
        User.objects.exclude(id__in=excluded_user_ids).filter(is_active=True)
    )

    sample_size = min(len(eligible_users), pool_size)
    if sample_size > 0:
        selected_jurors = random.sample(eligible_users, sample_size)
    else:
        selected_jurors = []

    for juror in selected_jurors:
        JuryAssignment.objects.get_or_create(
            dispute=dispute,
            juror=juror,
            tier=tier
        )

    if selected_jurors:
        if tier == 1:
            dispute.status = 'in_jury_review'
        else:
            dispute.status = 'appeal_review'
        dispute.save()

    return selected_jurors


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    user = request.user

    is_participant = user in [task.posted_by, task.taken_by]
    is_juror = JuryAssignment.objects.filter(dispute=dispute, juror=user).exists()

    if not is_participant and not is_juror and not user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    active_tier = 2 if dispute.status in ['appealed', 'appeal_review'] else 1

    user_is_active_juror = JuryAssignment.objects.filter(
        dispute=dispute, juror=user, tier=active_tier
    ).exists()

    has_voted = DisputeVote.objects.filter(
        dispute=dispute, voter=user, tier=active_tier
    ).exists()

    can_vote = user_is_active_juror and not has_voted and dispute.status in ['in_jury_review', 'appeal_review', 'appealed']

    tier1_votes_poster = DisputeVote.objects.filter(dispute=dispute, tier=1, choice='poster').count()
    tier1_votes_taker = DisputeVote.objects.filter(dispute=dispute, tier=1, choice='taker').count()
    tier2_votes_poster = DisputeVote.objects.filter(dispute=dispute, tier=2, choice='poster').count()
    tier2_votes_taker = DisputeVote.objects.filter(dispute=dispute, tier=2, choice='taker').count()

    bond_amount = getattr(settings, 'APPEAL_BOND_AMOUNT', getattr(task, 'reward', 100))

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'can_vote': can_vote,
        'has_voted': has_voted,
        'active_tier': active_tier,
        'tier1_votes_poster': tier1_votes_poster,
        'tier1_votes_taker': tier1_votes_taker,
        'tier2_votes_poster': tier2_votes_poster,
        'tier2_votes_taker': tier2_votes_taker,
        'is_appealable': dispute.is_appealable() and is_participant,
        'appeal_bond_amount': bond_amount,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task in progress that you participated in.")
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
                    status='open',
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

            assign_jury_pool(dispute, tier=1, pool_size=3)

            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully and submitted to jury review. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    choice = request.POST.get('choice')

    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status not in ['in_jury_review', 'appealed', 'appeal_review']:
        messages.error(request, "This dispute is not currently accepting votes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    active_tier = 1 if dispute.status in ['open', 'in_jury_review'] else 2

    if not JuryAssignment.objects.filter(dispute=dispute, juror=request.user, tier=active_tier).exists():
        messages.error(request, "You are not an assigned juror for this review phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user, tier=active_tier).exists():
        messages.error(request, "You have already cast a vote for this dispute phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    threshold = getattr(settings, 'SUPERMAJORITY_THRESHOLD', 0.66)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            choice=choice,
            tier=active_tier
        )

        total_votes = DisputeVote.objects.filter(dispute=dispute, tier=active_tier).count()
        poster_votes = DisputeVote.objects.filter(dispute=dispute, choice='poster', tier=active_tier).count()
        taker_votes = DisputeVote.objects.filter(dispute=dispute, choice='taker', tier=active_tier).count()

        total_assigned = JuryAssignment.objects.filter(dispute=dispute, tier=active_tier).count()

        min_quorum = min(2, total_assigned) if total_assigned > 0 else 1

        winning_choice = None
        if total_votes >= min_quorum:
            if (poster_votes / total_votes) >= threshold:
                winning_choice = 'poster'
            elif (taker_votes / total_votes) >= threshold:
                winning_choice = 'taker'

        if winning_choice:
            task = dispute.task
            winner_user = task.posted_by if winning_choice == 'poster' else task.taken_by

            if active_tier == 1:
                dispute.status = 'resolved'
                dispute.resolved_at = timezone.now()
                dispute.resolution_winner = winner_user
                dispute.initial_winner = winner_user
                dispute.save()

                if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                    if dispute.raised_by == winner_user:
                        dispute.refund_deposit(
                            reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=winner_user,
                            reason_description=f"Security deposit bond forfeited upon losing dispute for task: '{task.title}'"
                        )

                if winning_choice == 'taker' and task.taken_by:
                    task.status = 'completed'
                    task.save()

                    taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_payout',
                        description=f"Dispute resolved in favor of taker for task '{task.title}'"
                    )
                elif winning_choice == 'poster':
                    task.status = 'cancelled'
                    task.save()

                    poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_refund',
                        description=f"Dispute resolved in favor of poster for task '{task.title}'"
                    )

                # Reward majority Tier 1 jurors
                majority_voters = DisputeVote.objects.filter(
                    dispute=dispute, tier=1, choice=winning_choice
                )
                for vote_obj in majority_voters:
                    juror_profile, _ = UserProfile.objects.get_or_create(user=vote_obj.voter)
                    juror_profile.rewards += 10
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=vote_obj.voter,
                        task=task,
                        amount=10,
                        transaction_type='juror_reward',
                        description=f"Juror reward for dispute consensus on '{task.title}'"
                    )

            elif active_tier == 2:
                appeal_winner = winner_user
                initial_winner = dispute.initial_winner
                appellant = dispute.appellant
                bond_amount = dispute.appeal_bond_amount
                slash_penalty = getattr(settings, 'SLASH_PENALTY_AMOUNT', task.reward)

                if appeal_winner != initial_winner:
                    # OVERTURNED: Appellant was correct!
                    # 1. Refund appeal bond
                    if appellant:
                        appellant_profile, _ = UserProfile.objects.get_or_create(user=appellant)
                        appellant_profile.rewards += bond_amount
                        appellant_profile.save()
                        RewardLedger.objects.create(
                            user=appellant,
                            task=task,
                            amount=bond_amount,
                            transaction_type='appeal_refund',
                            description=f"Appeal bond refunded for dispute on '{task.title}'"
                        )

                    # 2. Reverse task settlement in favor of appeal winner
                    if appeal_winner == task.taken_by:
                        task.status = 'completed'
                        task.save()

                        # Reverse poster refund if poster was initial winner
                        poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                        poster_profile.rewards = max(0, poster_profile.rewards - task.reward)
                        poster_profile.save()

                        # Award reward to taker
                        taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='dispute_payout',
                            description=f"Dispute payout awarded after appeal overturn on '{task.title}'"
                        )
                    elif appeal_winner == task.posted_by:
                        task.status = 'cancelled'
                        task.save()

                        if task.taken_by:
                            taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                            taker_profile.rewards = max(0, taker_profile.rewards - task.reward)
                            taker_profile.save()

                        poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                        poster_profile.rewards += task.reward
                        poster_profile.save()

                        RewardLedger.objects.create(
                            user=task.posted_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='dispute_refund',
                            description=f"Dispute refund awarded after appeal overturn on '{task.title}'"
                        )

                    # 3. Slash initial winner (bad-actor litigant)
                    if initial_winner:
                        bad_actor_profile, _ = UserProfile.objects.get_or_create(user=initial_winner)
                        bad_actor_profile.rewards = max(0, bad_actor_profile.rewards - slash_penalty)
                        bad_actor_profile.save()
                        RewardLedger.objects.create(
                            user=initial_winner,
                            task=task,
                            amount=-slash_penalty,
                            transaction_type='slash_penalty',
                            description=f"Slash penalty incurred for bad-actor dispute on '{task.title}'"
                        )

                    # 4. Slash dishonest Tier 1 jurors
                    initial_winner_choice = 'poster' if initial_winner == task.posted_by else 'taker'
                    bad_jurors = DisputeVote.objects.filter(
                        dispute=dispute, tier=1, choice=initial_winner_choice
                    )
                    for bad_vote in bad_jurors:
                        j_profile, _ = UserProfile.objects.get_or_create(user=bad_vote.voter)
                        j_profile.rewards = max(0, j_profile.rewards - 20)
                        j_profile.save()
                        RewardLedger.objects.create(
                            user=bad_vote.voter,
                            task=task,
                            amount=-20,
                            transaction_type='juror_slash',
                            description=f"Juror slash penalty for incorrect initial vote on '{task.title}'"
                        )

                    dispute.status = 'slashed'
                    dispute.resolution_winner = appeal_winner
                    dispute.save()

                else:
                    # UPHELD: Initial decision was correct! Frivolous appeal.
                    if appellant:
                        appellant_profile, _ = UserProfile.objects.get_or_create(user=appellant)
                        appellant_profile.rewards = max(0, appellant_profile.rewards - slash_penalty)
                        appellant_profile.save()
                        RewardLedger.objects.create(
                            user=appellant,
                            task=task,
                            amount=-slash_penalty,
                            transaction_type='slash_penalty',
                            description=f"Slash penalty for frivolous appeal on '{task.title}'"
                        )

                    dispute.status = 'slashed'
                    dispute.save()

                # Reward majority Tier 2 jurors
                tier2_majority = DisputeVote.objects.filter(
                    dispute=dispute, tier=2, choice=winning_choice
                )
                for vote_obj in tier2_majority:
                    j_profile, _ = UserProfile.objects.get_or_create(user=vote_obj.voter)
                    j_profile.rewards += 10
                    j_profile.save()
                    RewardLedger.objects.create(
                        user=vote_obj.voter,
                        task=task,
                        amount=10,
                        transaction_type='juror_reward',
                        description=f"Juror reward for appeal consensus on '{task.title}'"
                    )

    messages.success(request, "Your vote has been recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can appeal this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appealable():
        messages.error(request, "This dispute is not eligible for appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    bond_amount = getattr(settings, 'APPEAL_BOND_AMOUNT', getattr(task, 'reward', 100))
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if user_profile.rewards < bond_amount:
        messages.error(request, f"Insufficient rewards balance. You need {bond_amount} points to file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-bond_amount,
            transaction_type='appeal_fee',
            description=f"Appeal bond posted for dispute on '{task.title}'"
        )

        dispute.appellant = request.user
        dispute.appeal_bond_amount = bond_amount
        dispute.status = 'appealed'
        dispute.save()

        assign_jury_pool(dispute, tier=2, pool_size=3)

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has appealed the dispute ruling for task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appeal submitted successfully. {bond_amount} points deposited as appeal bond.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    if dispute.status in ['resolved', 'slashed']:
        messages.error(request, "Resolved or finalized disputes cannot be withdrawn.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
