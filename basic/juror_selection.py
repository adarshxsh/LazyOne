import math
from django.db import transaction
from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse
from .models import Dispute, DisputeJuror, RewardLedger, Notification, UserProfile, Friendship, FriendRequest


def get_excluded_user_ids(dispute):
    """
    Returns a set of user IDs that cannot be selected as jurors for the dispute.
    Excludes:
    - Task poster (posted_by)
    - Task worker (taken_by)
    - Dispute raised_by
    - Direct social connections (friends, Friendship, FriendRequest) of poster or worker
    """
    excluded = set()

    counterparties = []
    if dispute.task.posted_by:
        counterparties.append(dispute.task.posted_by)
    if dispute.task.taken_by:
        counterparties.append(dispute.task.taken_by)
    if dispute.raised_by and dispute.raised_by not in counterparties:
        counterparties.append(dispute.raised_by)

    for cp in counterparties:
        excluded.add(cp.id)

        # UserProfile friends M2M (both directions)
        if hasattr(cp, 'userprofile'):
            profile = cp.userprofile
            # Friends in profile.friends
            for friend_profile in profile.friends.all():
                excluded.add(friend_profile.user_id)
            # Inverse friends: profiles that have cp's profile in their friends
            for inverse_profile in UserProfile.objects.filter(friends=profile):
                excluded.add(inverse_profile.user_id)

            # Friendship records (from_user or to_user)
            friendships_from = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            excluded.update(friendships_from)
            friendships_to = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            excluded.update(friendships_to)

        # FriendRequest records (from_user or to_user)
        reqs_from = FriendRequest.objects.filter(from_user=cp).values_list('to_user_id', flat=True)
        excluded.update(reqs_from)
        reqs_to = FriendRequest.objects.filter(to_user=cp).values_list('from_user_id', flat=True)
        excluded.update(reqs_to)

    return excluded


def select_and_assign_jurors(dispute, panel_size=3, stake_amount=50):
    """
    Dynamically queries eligible verified users, filtering out counterparties/friends,
    checks reward balance (>= 100 and stake_amount <= 20% of rewards),
    and transactionally locks per-dispute stake upon assignment.
    If < panel_size eligible jurors are available, transitions dispute status to 'pending_staff_review'.
    """
    min_required_balance = max(100, math.ceil(stake_amount / 0.20))

    excluded_user_ids = get_excluded_user_ids(dispute)

    candidate_qs = User.objects.filter(
        is_active=True
    ).filter(
        Q(userprofile__is_phone_verified=True) | Q(userprofile__is_instagram_verified=True)
    ).filter(
        userprofile__rewards__gte=min_required_balance
    ).exclude(
        id__in=excluded_user_ids
    ).distinct()

    candidates = list(candidate_qs.order_by('?')[:panel_size])

    with transaction.atomic():
        if len(candidates) < panel_size:
            dispute.status = 'pending_staff_review'
            dispute.save()

            recipients = set([dispute.task.posted_by])
            if dispute.task.taken_by:
                recipients.add(dispute.task.taken_by)

            for recipient in recipients:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"Dispute for task '{dispute.task.title}' could not find enough neutral jurors and has been assigned to staff review.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            return False

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for juror_user in candidates:
            juror_profile = juror_user.userprofile
            juror_profile.rewards -= stake_amount
            juror_profile.save()

            DisputeJuror.objects.create(
                dispute=dispute,
                user=juror_user,
                stake_amount=stake_amount,
                is_stake_locked=True
            )

            RewardLedger.objects.create(
                user=juror_user,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='juror_stake_held',
                description=f"Locked dispute juror stake for task: '{dispute.task.title}'"
            )

            Notification.objects.create(
                recipient=juror_user,
                message=f"You have been assigned as a juror for dispute on task '{dispute.task.title}'. A stake of {stake_amount} points has been locked.",
                link=dispute_link
            )

        return True


def unlock_dispute_juror_stakes(dispute, reason_description=None):
    """
    Refunds locked stakes to all assigned jurors of a dispute (e.g. on dispute withdrawal or resolution).
    """
    locked_jurors = dispute.jurors.filter(is_stake_locked=True)
    for juror in locked_jurors:
        with transaction.atomic():
            user_profile = juror.user.userprofile
            user_profile.rewards += juror.stake_amount
            user_profile.save()

            desc = reason_description or f"Refunded juror stake for dispute on task: '{dispute.task.title}'"
            RewardLedger.objects.create(
                user=juror.user,
                task=dispute.task,
                amount=juror.stake_amount,
                transaction_type='juror_stake_refunded',
                description=desc
            )

            juror.is_stake_locked = False
            juror.save()

            Notification.objects.create(
                recipient=juror.user,
                message=f"Your locked stake of {juror.stake_amount} points for dispute on task '{dispute.task.title}' has been refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
