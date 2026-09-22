import random
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from .models import UserProfile, Friendship, RewardLedger, JurorAssignment


def get_eligible_juror_candidates(dispute, min_balance=50):
    """
    Returns a QuerySet of User candidates eligible for juror selection for a given dispute.
    Filters out:
    - Task poster and task taker.
    - Active friends of task poster or task taker (via ManyToMany and Friendship model).
    - Inactive users or users whose last_login is not within the past 30 days.
    - Users with a reward balance less than min_balance (default: 50).
    """
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    excluded_user_ids = set()
    if poster:
        excluded_user_ids.add(poster.id)
    if taker:
        excluded_user_ids.add(taker.id)

    # Collect friends of poster
    if poster:
        try:
            poster_profile = poster.userprofile
            m2m_poster_friends = poster_profile.friends.all().values_list('user_id', flat=True)
            excluded_user_ids.update(m2m_poster_friends)

            fs_poster_from = Friendship.objects.filter(from_user=poster_profile).values_list('to_user__user_id', flat=True)
            excluded_user_ids.update(fs_poster_from)

            fs_poster_to = Friendship.objects.filter(to_user=poster_profile).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(fs_poster_to)
        except UserProfile.DoesNotExist:
            pass

    # Collect friends of taker
    if taker:
        try:
            taker_profile = taker.userprofile
            m2m_taker_friends = taker_profile.friends.all().values_list('user_id', flat=True)
            excluded_user_ids.update(m2m_taker_friends)

            fs_taker_from = Friendship.objects.filter(from_user=taker_profile).values_list('to_user__user_id', flat=True)
            excluded_user_ids.update(fs_taker_from)

            fs_taker_to = Friendship.objects.filter(to_user=taker_profile).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(fs_taker_to)
        except UserProfile.DoesNotExist:
            pass

    # Candidates must have logged in within 30 days
    cutoff_time = timezone.now() - timedelta(days=30)

    eligible_qs = User.objects.filter(
        is_active=True,
        last_login__gte=cutoff_time,
        userprofile__rewards__gte=min_balance
    ).exclude(id__in=excluded_user_ids)

    return eligible_qs


def assign_jurors_to_dispute(dispute, panel_size=3, min_balance=50, stake_amount=10):
    """
    Randomly selects panel_size candidates from eligible users, locks a stake_amount
    (default 10 points) in escrow from each selected juror, and creates JurorAssignment records.
    """
    eligible_qs = get_eligible_juror_candidates(dispute, min_balance=min_balance)
    # Exclude already assigned jurors if any
    already_assigned_ids = dispute.juror_assignments.values_list('juror_id', flat=True)
    eligible_qs = eligible_qs.exclude(id__in=already_assigned_ids)

    eligible_list = list(eligible_qs)

    if len(eligible_list) < panel_size:
        selected_candidates = eligible_list
    else:
        selected_candidates = random.sample(eligible_list, panel_size)

    assignments = []
    with transaction.atomic():
        for candidate in selected_candidates:
            # Deduct stake from candidate's reward balance
            candidate_profile = candidate.userprofile
            candidate_profile.rewards -= stake_amount
            candidate_profile.save()

            # Record transaction in RewardLedger
            RewardLedger.objects.create(
                user=candidate,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='juror_stake',
                description=f"Juror deposit bond held for dispute on task: '{dispute.task.title}'"
            )

            # Create assignment
            assignment = JurorAssignment.objects.create(
                dispute=dispute,
                juror=candidate,
                stake_amount=stake_amount,
                stake_status='held'
            )
            assignments.append(assignment)

    return assignments


def submit_juror_vote(assignment, voted_for):
    """
    Submits a vote for the given JurorAssignment and refunds the juror's stake_amount.
    """
    if assignment.voted:
        return assignment

    with transaction.atomic():
        assignment.voted = True
        assignment.voted_for = voted_for
        assignment.voted_at = timezone.now()

        if assignment.stake_status == 'held':
            # Refund 10 points stake to juror
            juror_profile = assignment.juror.userprofile
            juror_profile.rewards += assignment.stake_amount
            juror_profile.save()

            # Record refund in RewardLedger
            RewardLedger.objects.create(
                user=assignment.juror,
                task=assignment.dispute.task,
                amount=assignment.stake_amount,
                transaction_type='juror_stake_refund',
                description=f"Juror deposit bond refunded for voting in dispute on task: '{assignment.dispute.task.title}'"
            )

            assignment.stake_status = 'refunded'

        assignment.save()

    return assignment
