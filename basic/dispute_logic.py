from django.utils import timezone
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from .models import Dispute, DisputeVote, RewardLedger, UserProfile, Notification
from django.urls import reverse

def is_eligible_juror(user, dispute):
    if not user or not user.is_authenticated:
        return False
    
    task = dispute.task
    if user == task.posted_by or user == task.taken_by:
        return False

    user_profile = getattr(user, 'userprofile', None)
    if not user_profile:
        return False

    poster_profile = getattr(task.posted_by, 'userprofile', None)
    taker_profile = getattr(task.taken_by, 'userprofile', None)

    if poster_profile:
        if user_profile.friends.filter(id=poster_profile.id).exists() or poster_profile.friends.filter(id=user_profile.id).exists():
            return False
    if taker_profile:
        if user_profile.friends.filter(id=taker_profile.id).exists() or taker_profile.friends.filter(id=user_profile.id).exists():
            return False

    return True

def evaluate_dispute_state(dispute):
    now = timezone.now()

    # Stage 1: Primary Voting
    if dispute.status in ['open', 'voting_primary']:
        primary_votes = dispute.votes.filter(stage='primary')
        total_votes = primary_votes.count()
        poster_wins = primary_votes.filter(vote='poster_wins').count()
        taker_wins = primary_votes.filter(vote='taker_wins').count()

        is_expired = dispute.primary_voting_ends_at and now >= dispute.primary_voting_ends_at

        # If expired or quorum reached with majority
        if is_expired or total_votes >= 3:
            if total_votes < 3:
                dispute.status = 'escalated_staff'
                dispute.save()
            else:
                if poster_wins > total_votes / 2:
                    winner = 'poster_wins'
                elif taker_wins > total_votes / 2:
                    winner = 'taker_wins'
                else:
                    winner = None

                if winner:
                    dispute.primary_winner = winner
                    dispute.status = 'appeal_window'
                    dispute.appeal_window_expires_at = now + timedelta(hours=24)
                    dispute.save()
                else:
                    if is_expired:
                        dispute.status = 'escalated_staff'
                        dispute.save()

    # Stage 2: Appeal Window Expiration
    elif dispute.status == 'appeal_window':
        if dispute.appeal_window_expires_at and now >= dispute.appeal_window_expires_at:
            if dispute.primary_winner:
                final_settlement(dispute, final_winner=dispute.primary_winner, stage='primary')

    # Stage 3: Appeal Voting
    elif dispute.status == 'appeal_pending':
        appeal_votes = dispute.votes.filter(stage='appeal')
        total_votes = appeal_votes.count()
        poster_wins = appeal_votes.filter(vote='poster_wins').count()
        taker_wins = appeal_votes.filter(vote='taker_wins').count()

        is_expired = dispute.appeal_voting_ends_at and now >= dispute.appeal_voting_ends_at

        if is_expired or total_votes >= 7:
            if total_votes < 7:
                dispute.status = 'escalated_staff'
                dispute.save()
            else:
                # Supermajority >= 66%
                if poster_wins * 100 >= 66 * total_votes:
                    final_winner = 'poster_wins'
                elif taker_wins * 100 >= 66 * total_votes:
                    final_winner = 'taker_wins'
                else:
                    final_winner = None

                if final_winner:
                    final_settlement(dispute, final_winner=final_winner, stage='appeal')
                else:
                    if is_expired:
                        dispute.status = 'escalated_staff'
                        dispute.save()

def file_appeal(dispute, user):
    now = timezone.now()
    if dispute.status != 'appeal_window':
        return False, "Dispute is not currently in the appeal window."

    if dispute.appeal_window_expires_at and now > dispute.appeal_window_expires_at:
        evaluate_dispute_state(dispute)
        return False, "The appeal window has expired."

    task = dispute.task
    if user != task.posted_by and user != task.taken_by:
        return False, "Only task litigants can file an appeal."

    bond_amount = max(1, int(0.20 * task.reward))
    user_profile = getattr(user, 'userprofile', None)
    if not user_profile or user_profile.rewards < bond_amount:
        return False, f"Insufficient balance for appeal bond. Required: {bond_amount} points."

    with transaction.atomic():
        user_profile.rewards -= bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=user,
            task=task,
            amount=-bond_amount,
            transaction_type='appeal_bond',
            description=f"Appeal bond deposit for task '{task.title}'"
        )

        dispute.appellant = user
        dispute.appeal_bond_amount = bond_amount
        dispute.status = 'appeal_pending'
        dispute.appeal_voting_ends_at = now + timedelta(hours=48)
        dispute.save()

    return True, f"Appeal filed successfully. Bond of {bond_amount} points deposited."

def final_settlement(dispute, final_winner, stage):
    task = dispute.task
    with transaction.atomic():
        dispute.final_winner = final_winner
        dispute.status = 'resolved'

        # 0. Initial Security Deposit Bond Handling
        winning_litigant = task.taken_by if final_winner == 'taker_wins' else task.posted_by
        if dispute.raised_by == winning_litigant:
            dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon winning dispute on task: '{task.title}'")
        else:
            dispute.forfeit_deposit(beneficiary=winning_litigant, reason_description=f"Security deposit bond forfeited upon losing dispute on task: '{task.title}'")

        # 1. Escrow Reward Settlement
        if final_winner == 'taker_wins':
            taker = task.taken_by
            taker_profile = taker.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=taker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Completed task via dispute settlement: '{task.title}'"
            )
            task.status = 'completed'
            task.save()
        else: # poster_wins
            poster = task.posted_by
            poster_profile = poster.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refunded task via dispute settlement: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

        # 2. Appeal Bond Handling
        bond_pool = 0
        if dispute.appellant:
            appellant = dispute.appellant
            appellant_won = (final_winner == 'poster_wins' and appellant == task.posted_by) or \
                            (final_winner == 'taker_wins' and appellant == task.taken_by)
            if appellant_won:
                app_profile = appellant.userprofile
                app_profile.rewards += dispute.appeal_bond_amount
                app_profile.save()
                RewardLedger.objects.create(
                    user=appellant,
                    task=task,
                    amount=dispute.appeal_bond_amount,
                    transaction_type='governance_reward',
                    description=f"Refund of appeal bond for task '{task.title}'"
                )
            else:
                bond_pool = dispute.appeal_bond_amount

        # 3. Bad-faith Litigant Slashing
        losing_litigant = task.posted_by if final_winner == 'taker_wins' else task.taken_by
        litigant_slash = max(1, int(0.20 * task.reward))
        losing_profile = losing_litigant.userprofile
        actual_litigant_slash = max(0, min(losing_profile.rewards, litigant_slash))
        if actual_litigant_slash > 0:
            losing_profile.rewards -= actual_litigant_slash
            losing_profile.save()
            RewardLedger.objects.create(
                user=losing_litigant,
                task=task,
                amount=-actual_litigant_slash,
                transaction_type='litigant_slashing',
                description=f"Bad-faith litigant slashing penalty for task '{task.title}'"
            )

        # 4. Juror Slashing
        stage_votes = dispute.votes.filter(stage=stage)
        aligned_votes = stage_votes.filter(vote=final_winner)
        unaligned_votes = stage_votes.exclude(vote=final_winner)

        juror_slash = max(1, int(0.05 * task.reward))
        total_juror_slash_pool = 0

        for uv in unaligned_votes:
            u_juror = uv.juror
            u_profile = u_juror.userprofile
            u_actual = max(0, min(u_profile.rewards, juror_slash))
            if u_actual > 0:
                u_profile.rewards -= u_actual
                u_profile.save()
                RewardLedger.objects.create(
                    user=u_juror,
                    task=task,
                    amount=-u_actual,
                    transaction_type='juror_slashing',
                    description=f"Unaligned juror slashing penalty for task '{task.title}'"
                )
                total_juror_slash_pool += u_actual

        # 5. Governance Rewards Distribution
        total_reward_pool = bond_pool + actual_litigant_slash + total_juror_slash_pool
        aligned_count = aligned_votes.count()

        if aligned_count > 0 and total_reward_pool > 0:
            per_juror_reward = total_reward_pool // aligned_count
            for av in aligned_votes:
                a_juror = av.juror
                a_profile = a_juror.userprofile
                a_profile.rewards += per_juror_reward
                a_profile.save()
                RewardLedger.objects.create(
                    user=a_juror,
                    task=task,
                    amount=per_juror_reward,
                    transaction_type='governance_reward',
                    description=f"Governance reward for aligned jury vote on task '{task.title}'"
                )

        dispute.save()
