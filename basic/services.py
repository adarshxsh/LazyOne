import random
from django.db import transaction
from django.db.models import Count
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Dispute, Task, RewardLedger, Friendship, Notification

def get_direct_friend_user_ids(user):
    """
    Returns a set of User IDs for all direct friends of the given user.
    """
    if not user or not hasattr(user, 'userprofile'):
        return set()
    user_profile = user.userprofile
    friends = set(user_profile.friends.all().values_list('user__id', flat=True))

    fs_from = Friendship.objects.filter(from_user=user_profile).values_list('to_user__user__id', flat=True)
    fs_to = Friendship.objects.filter(to_user=user_profile).values_list('from_user__user__id', flat=True)
    friends.update(fs_from)
    friends.update(fs_to)

    friends.discard(user.id)
    return friends

def select_and_lock_jurors(dispute, panel_size=3, stake_amount=100):
    """
    Dynamically selects qualified, non-conflicted active users as jurors and locks their stake.
    Returns True if successfully selected and locked, or False if pool size is insufficient.
    """
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    # 1. Counterparty exclusion
    excluded_user_ids = {poster.id}
    if taker:
        excluded_user_ids.add(taker.id)

    # 2. Direct friends exclusion for poster and taker
    poster_friends = get_direct_friend_user_ids(poster)
    excluded_user_ids.update(poster_friends)
    if taker:
        taker_friends = get_direct_friend_user_ids(taker)
        excluded_user_ids.update(taker_friends)

    # 3. Active disputes limit: exclude users currently serving as jurors on >= 2 open disputes
    open_disputes = Dispute.objects.filter(status='open')
    busy_juror_ids = (
        open_disputes.values('jurors')
        .annotate(active_count=Count('id'))
        .filter(active_count__gte=2)
        .values_list('jurors', flat=True)
    )
    excluded_user_ids.update(busy_juror_ids)

    # 4. Filter qualified users with >= stake_amount reward balance
    qualified_user_ids = list(
        User.objects.filter(is_active=True, userprofile__rewards__gte=stake_amount)
        .exclude(id__in=excluded_user_ids)
        .values_list('id', flat=True)
    )

    if len(qualified_user_ids) < panel_size:
        return False

    # 5. Random selection
    selected_ids = random.sample(qualified_user_ids, panel_size)
    selected_users = list(User.objects.filter(id__in=selected_ids))

    # 6. Lock stake and assign jurors
    for juror in selected_users:
        juror_profile = juror.userprofile
        juror_profile.rewards -= stake_amount
        juror_profile.save()

        RewardLedger.objects.create(
            user=juror,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake_lock',
            description=f"Locked {stake_amount} points stake as juror for dispute on task: '{task.title}'"
        )
        dispute.jurors.add(juror)

        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a juror for dispute on task '{task.title}'. {stake_amount} points locked as stake.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return True

def resolve_dispute(dispute, stake_amount=100):
    """
    Settles dispute outcome, refunds/awards task rewards, and redistributes juror stakes.
    """
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    poster_votes = dispute.votes.filter(voted_for=poster).count()
    taker_votes = dispute.votes.filter(voted_for=taker).count()

    if poster_votes >= taker_votes:
        winner = poster
    else:
        winner = taker

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.winner = winner
        dispute.save()

        # Task settlement
        if winner == poster:
            task.status = 'cancelled'
            poster_profile = poster.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for task '{task.title}' resolved in your favor in dispute"
            )
        else:
            task.status = 'completed'
            if taker:
                taker_profile = taker.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=taker,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward for task '{task.title}' resolved in your favor in dispute"
                )
        task.save()

        # Juror stake settlement
        jurors = list(dispute.jurors.all())
        majority_votes = dispute.votes.filter(voted_for=winner)
        majority_voters = [v.voter for v in majority_votes]

        voted_voter_ids = set(v.voter.id for v in dispute.votes.all())
        slashed_voters = [j for j in jurors if j not in majority_voters]

        slashed_count = len(slashed_voters)
        total_slashed_stake = slashed_count * stake_amount

        for s_voter in slashed_voters:
            RewardLedger.objects.create(
                user=s_voter,
                task=task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Stake of {stake_amount} points slashed in dispute on task '{task.title}'"
            )
            Notification.objects.create(
                recipient=s_voter,
                message=f"Dispute resolved for task '{task.title}'. Your {stake_amount} points stake was slashed.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        majority_count = len(majority_voters)
        reward_share = (total_slashed_stake // majority_count) if majority_count > 0 else 0

        for m_voter in majority_voters:
            m_profile = m_voter.userprofile
            # Return stake
            m_profile.rewards += stake_amount
            RewardLedger.objects.create(
                user=m_voter,
                task=task,
                amount=stake_amount,
                transaction_type='juror_stake_return',
                description=f"Returned {stake_amount} points stake for dispute on task '{task.title}'"
            )
            # Reward share from slashed stake
            if reward_share > 0:
                m_profile.rewards += reward_share
                RewardLedger.objects.create(
                    user=m_voter,
                    task=task,
                    amount=reward_share,
                    transaction_type='juror_reward',
                    description=f"Earned {reward_share} reward share from slashed stake in dispute on task '{task.title}'"
                )
            m_profile.save()

            Notification.objects.create(
                recipient=m_voter,
                message=f"Dispute resolved for task '{task.title}'. Stake returned + {reward_share} reward share earned!",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        # Notify counterparties
        Notification.objects.create(
            recipient=poster,
            message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if taker:
            Notification.objects.create(
                recipient=taker,
                message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
