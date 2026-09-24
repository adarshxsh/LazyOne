import math
from django.db import transaction
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Dispute, Jury, Juror, Vote, RewardLedger, Notification


def select_and_assign_jurors(dispute, panel_size=3, stake_amount=50):
    """
    Select eligible community users to serve as jurors for a dispute.
    Excludes task participants (posted_by, taken_by) and dispute raiser.
    Requires userprofile.rewards >= stake_amount.
    """
    excluded_ids = set()
    if dispute.task.posted_by_id:
        excluded_ids.add(dispute.task.posted_by_id)
    if dispute.task.taken_by_id:
        excluded_ids.add(dispute.task.taken_by_id)
    if dispute.raised_by_id:
        excluded_ids.add(dispute.raised_by_id)

    candidates = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=stake_amount
    ).exclude(id__in=excluded_ids).order_by('id')[:panel_size]

    return list(candidates)


def create_jury_for_dispute(dispute, panel_size=3, stake_amount=50):
    """
    Instantiates or retrieves the Jury for a dispute, selects jurors, locks their stakes,
    calculates juror voting weights, and sends notifications.
    """
    with transaction.atomic():
        jury, created = Jury.objects.get_or_create(
            dispute=dispute,
            defaults={
                'quorum_required': panel_size,
                'status': 'pending'
            }
        )

        if jury.jurors.exists():
            return jury

        candidates = select_and_assign_jurors(dispute, panel_size=panel_size, stake_amount=stake_amount)

        if len(candidates) < panel_size:
            jury.status = 'fallback'
            jury.save()
            return jury

        for user in candidates:
            rewards = getattr(user.userprofile, 'rewards', 1500)
            weight = max(1.0, round(rewards / 500.0, 2))

            profile = user.userprofile
            profile.rewards -= stake_amount
            profile.save()

            RewardLedger.objects.create(
                user=user,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='juror_stake',
                description=f"Stake locked for serving as juror on dispute for task: '{dispute.task.title}'"
            )

            Juror.objects.create(
                jury=jury,
                user=user,
                weight=weight,
                staked_amount=stake_amount,
                status='assigned'
            )

            Notification.objects.create(
                recipient=user,
                message=f"You have been selected as a juror for a dispute on task: '{dispute.task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        jury.status = 'voting'
        jury.save()
        return jury


def cast_juror_vote(juror, choice, reasoning=""):
    """
    Casts a vote for an assigned juror.
    Choice must be 'poster' or 'taker'.
    Executes within atomic transaction and triggers consensus aggregation.
    """
    if choice not in ['poster', 'taker']:
        raise ValueError("Invalid vote choice. Must be 'poster' or 'taker'.")

    if juror.status != 'assigned':
        raise ValueError("Juror has already voted or is not eligible.")

    jury = juror.jury
    if jury.status != 'voting' or juror.jury.dispute.status != 'open':
        raise ValueError("Jury voting is not open for this dispute.")

    with transaction.atomic():
        Vote.objects.create(
            jury=jury,
            juror=juror,
            choice=choice,
            weight=juror.weight,
            reasoning=reasoning
        )
        juror.status = 'voted'
        juror.save()

        check_and_aggregate_consensus(jury)


def check_and_aggregate_consensus(jury):
    """
    Evaluates weighted votes cast on a jury.
    If supermajority or quorum threshold is reached, settles the dispute,
    adjusts task state, distributes juror rewards, slashes losing jurors, and notifies users.
    """
    votes = list(jury.votes.all())
    if not votes:
        return False

    poster_weight = sum(v.weight for v in votes if v.choice == 'poster')
    taker_weight = sum(v.weight for v in votes if v.choice == 'taker')
    total_voted_weight = poster_weight + taker_weight

    if total_voted_weight == 0:
        return False

    total_possible_weight = sum(j.weight for j in jury.jurors.all())
    poster_possible_ratio = poster_weight / total_possible_weight if total_possible_weight > 0 else 0
    taker_possible_ratio = taker_weight / total_possible_weight if total_possible_weight > 0 else 0

    total_jurors_count = jury.jurors.count()
    voted_count = len(votes)

    winning_outcome = None

    if poster_possible_ratio >= jury.supermajority_threshold:
        winning_outcome = 'poster'
    elif taker_possible_ratio >= jury.supermajority_threshold:
        winning_outcome = 'taker'
    elif voted_count >= total_jurors_count or voted_count >= jury.quorum_required:
        if poster_weight > taker_weight:
            winning_outcome = 'poster'
        elif taker_weight > poster_weight:
            winning_outcome = 'taker'
        else:
            winning_outcome = 'draw'

    if not winning_outcome:
        return False

    dispute = jury.dispute
    task = dispute.task

    with transaction.atomic():
        jury.consensus_reached = True
        jury.consensus_outcome = f"{winning_outcome}_win" if winning_outcome != 'draw' else 'draw'
        jury.status = 'resolved'
        jury.save()

        dispute.status = 'resolved'
        dispute.save()

        if winning_outcome == 'poster':
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
                description=f"Task reward refunded after winning dispute on task: '{task.title}'"
            )

            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(reason_description=f"Security deposit refunded after winning dispute on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit forfeited to poster after taker lost dispute on task: '{task.title}'")

        elif winning_outcome == 'taker':
            task.status = 'completed'
            task.save()

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded after winning dispute on task: '{task.title}'"
            )

            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(reason_description=f"Security deposit refunded after winning dispute on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit forfeited to taker after poster lost dispute on task: '{task.title}'")

        elif winning_outcome == 'draw':
            poster_share = task.reward // 2
            taker_share = task.reward - poster_share

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += poster_share
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_share,
                transaction_type='task_cancellation',
                description=f"50% split reward returned for draw dispute on task: '{task.title}'"
            )

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += taker_share
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_share,
                transaction_type='task_completion',
                description=f"50% split reward awarded for draw dispute on task: '{task.title}'"
            )

            task.status = 'completed'
            task.save()
            dispute.refund_deposit(reason_description=f"Deposit refunded for draw dispute on task: '{task.title}'")

        losing_choice = 'taker' if winning_outcome == 'poster' else ('poster' if winning_outcome == 'taker' else None)
        losing_jurors = [j for j in jury.jurors.all() if j.votes.filter(choice=losing_choice).exists()] if losing_choice else []
        slashed_pool = sum(j.staked_amount for j in losing_jurors)

        winning_jurors = [j for j in jury.jurors.all() if j.votes.filter(choice=winning_outcome).exists()] if winning_outcome != 'draw' else list(jury.jurors.all())
        winning_weight_sum = sum(j.weight for j in winning_jurors) if winning_jurors else 0

        for juror in jury.jurors.all():
            j_vote = juror.votes.first()

            if not j_vote or (losing_choice and j_vote.choice == losing_choice):
                juror.status = 'slashed'
                juror.save()
                RewardLedger.objects.create(
                    user=juror.user,
                    task=task,
                    amount=0,
                    transaction_type='juror_slash',
                    description=f"Stake slashed for dissenting/missing vote on dispute for task: '{task.title}'"
                )
            else:
                reward_bonus = 0
                if winning_weight_sum > 0 and slashed_pool > 0:
                    reward_bonus = math.floor(slashed_pool * (juror.weight / winning_weight_sum))

                total_return = juror.staked_amount + reward_bonus
                profile = juror.user.userprofile
                profile.rewards += total_return
                profile.save()

                juror.status = 'rewarded'
                juror.save()

                RewardLedger.objects.create(
                    user=juror.user,
                    task=task,
                    amount=total_return,
                    transaction_type='juror_reward',
                    description=f"Stake returned ({juror.staked_amount}) plus reward bonus ({reward_bonus}) for consensus vote on task: '{task.title}'"
                )

        outcome_desc = "Poster Wins" if winning_outcome == 'poster' else ("Taker Wins" if winning_outcome == 'taker' else "Draw")
        msg = f"Dispute for task '{task.title}' has been resolved by jury consensus: {outcome_desc}."

        if task.posted_by:
            Notification.objects.create(recipient=task.posted_by, message=msg, link=reverse('dispute_detail', args=[dispute.id]))
        if task.taken_by:
            Notification.objects.create(recipient=task.taken_by, message=msg, link=reverse('dispute_detail', args=[dispute.id]))
        for juror in jury.jurors.all():
            Notification.objects.create(recipient=juror.user, message=msg, link=reverse('dispute_detail', args=[dispute.id]))

        return True


def refund_juror_stakes_on_withdrawal(jury):
    """
    Refunds locked stakes to all assigned jurors if a dispute is withdrawn or settled directly.
    """
    if not jury or jury.status == 'resolved':
        return

    with transaction.atomic():
        for juror in jury.jurors.all():
            if juror.status in ['assigned', 'voted']:
                profile = juror.user.userprofile
                profile.rewards += juror.staked_amount
                profile.save()

                RewardLedger.objects.create(
                    user=juror.user,
                    task=jury.dispute.task,
                    amount=juror.staked_amount,
                    transaction_type='juror_stake_refund',
                    description=f"Juror stake refunded due to dispute withdrawal/settlement on task: '{jury.dispute.task.title}'"
                )
                juror.status = 'slashed'
                juror.save()

        jury.status = 'resolved'
        jury.save()
