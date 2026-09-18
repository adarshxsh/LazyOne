import random
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from .models import (
    User, UserProfile, Dispute, Task, Notification,
    JuryPool, JuryAssignment, DisputeVote, RewardLedger
)

JURY_VOTE_REWARD = 10


def sync_jury_pool():
    """Ensure all users with UserProfile are enrolled in JuryPool."""
    user_profiles = UserProfile.objects.select_related('user').all()
    for profile in user_profiles:
        JuryPool.objects.get_or_create(user=profile.user, defaults={'is_eligible': True})


def assign_jurors(dispute, count=5):
    """
    Select an odd number of eligible, non-conflicted peer jurors randomly.
    Direct task participants and their friends are excluded.
    """
    if not dispute.voting_deadline:
        dispute.voting_deadline = timezone.now() + timedelta(hours=72)
        dispute.save()

    sync_jury_pool()
    task = dispute.task

    # Exclude direct participants
    excluded_user_ids = {task.posted_by.id}
    if task.taken_by:
        excluded_user_ids.add(task.taken_by.id)

    # Exclude friends of task.posted_by
    if hasattr(task.posted_by, 'userprofile'):
        posted_by_friends = task.posted_by.userprofile.friends.all().values_list('user_id', flat=True)
        excluded_user_ids.update(posted_by_friends)

    # Exclude friends of task.taken_by
    if task.taken_by and hasattr(task.taken_by, 'userprofile'):
        taken_by_friends = task.taken_by.userprofile.friends.all().values_list('user_id', flat=True)
        excluded_user_ids.update(taken_by_friends)

    # Eligible jurors
    eligible_juror_users = list(
        JuryPool.objects.filter(is_eligible=True)
        .exclude(user_id__in=excluded_user_ids)
        .values_list('user_id', flat=True)
    )

    N = len(eligible_juror_users)
    num_to_pick = min(count, N)
    if num_to_pick > 0 and num_to_pick % 2 == 0:
        num_to_pick -= 1

    if num_to_pick <= 0:
        return []

    selected_user_ids = random.sample(eligible_juror_users, num_to_pick)
    selected_users = User.objects.filter(id__in=selected_user_ids)

    assignments = []
    for user in selected_users:
        assignment, created = JuryAssignment.objects.get_or_create(
            dispute=dispute,
            juror=user
        )
        if created:
            Notification.objects.create(
                recipient=user,
                message=f"You have been selected as a juror for dispute on task '{task.title}'.",
                link=reverse('jury_dispute_detail', args=[dispute.id])
            )
        assignments.append(assignment)

    return assignments


def cast_juror_vote(juror, dispute, vote_choice, rationale=""):
    """
    Record a vote cast by an assigned juror, grant reward points,
    and trigger vote aggregation check.
    """
    assignment = JuryAssignment.objects.filter(dispute=dispute, juror=juror).first()
    if not assignment or assignment.has_voted or dispute.status == 'resolved':
        return False

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=juror,
            vote=vote_choice,
            rationale=rationale
        )
        assignment.has_voted = True
        assignment.save()

        # Award juror reward points for active participation
        if hasattr(juror, 'userprofile'):
            juror_profile = juror.userprofile
            juror_profile.rewards += JURY_VOTE_REWARD
            juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=dispute.task,
                amount=JURY_VOTE_REWARD,
                transaction_type='jury_reward',
                description=f"Jury voting reward for dispute on task '{dispute.task.title}'"
            )

    check_and_aggregate_dispute(dispute)
    return True


def check_and_aggregate_dispute(dispute):
    """
    Check if quorum/majority is reached or deadline has passed.
    Aggregate votes, update dispute and task statuses, execute payouts,
    and disqualify jurors who failed to vote before deadline.
    """
    if dispute.status == 'resolved':
        return dispute.winning_party

    total_assigned = dispute.jury_assignments.count()
    votes_count = dispute.votes.count()
    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()

    threshold = (total_assigned // 2) + 1 if total_assigned > 0 else 1
    is_deadline_passed = dispute.voting_deadline and timezone.now() >= dispute.voting_deadline
    has_majority = (poster_votes >= threshold or taker_votes >= threshold)
    all_voted = (total_assigned > 0 and votes_count == total_assigned)

    if not (has_majority or all_voted or is_deadline_passed):
        return None

    # Handle deadline expiration guardrail: disqualifying non-voting jurors
    if is_deadline_passed and not (has_majority or all_voted):
        non_voting_user_ids = dispute.jury_assignments.filter(has_voted=False).values_list('juror_id', flat=True)
        JuryPool.objects.filter(user_id__in=non_voting_user_ids).update(is_eligible=False)

    # Determine winner
    if poster_votes > taker_votes:
        winner = 'poster'
    elif taker_votes > poster_votes:
        winner = 'taker'
    else:
        winner = 'poster'  # Default fallback if tie

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.winning_party = winner
        dispute.save()

        task = dispute.task
        if winner == 'poster':
            task.status = 'cancelled'
            task.save()

            if task.posted_by and hasattr(task.posted_by, 'userprofile'):
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_refund',
                    description=f"Dispute resolved in favor of poster for task: '{task.title}'"
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in your favor. {task.reward} points refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of task poster.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        elif winner == 'taker':
            task.status = 'completed'
            task.save()

            if task.taken_by and hasattr(task.taken_by, 'userprofile'):
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_payout',
                    description=f"Dispute resolved in favor of taker for task: '{task.title}'"
                )
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved in your favor! {task.reward} points awarded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.posted_by:
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of task taker.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    return winner
