import random
from django.db import transaction
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Dispute, JuryPool, JurorAssignment, UserProfile, RewardLedger, Notification, Friendship, FriendRequest

def get_conflict_user_ids(dispute):
    """
    Returns a set of User IDs that have a conflict of interest with the dispute,
    including the task poster, task taker, dispute raiser, and all of their direct friends.
    """
    conflict_user_ids = set()
    
    task = dispute.task
    participants = [task.posted_by, task.taken_by, dispute.raised_by]
    
    for user in participants:
        if not user:
            continue
        conflict_user_ids.add(user.id)
        
        # 1. Friends in UserProfile.friends ManyToMany
        if hasattr(user, 'userprofile'):
            friends_ids = user.userprofile.friends.values_list('user_id', flat=True)
            conflict_user_ids.update(friends_ids)
            
            # Reverse M2M relationship
            reverse_friends_ids = UserProfile.objects.filter(friends=user.userprofile).values_list('user_id', flat=True)
            conflict_user_ids.update(reverse_friends_ids)
            
            # 2. Friendship model
            f_from = Friendship.objects.filter(from_user=user.userprofile).values_list('to_user__user_id', flat=True)
            f_to = Friendship.objects.filter(to_user=user.userprofile).values_list('from_user__user_id', flat=True)
            conflict_user_ids.update(f_from)
            conflict_user_ids.update(f_to)

        # 3. FriendRequest model (accepted)
        fr_from = FriendRequest.objects.filter(from_user=user, is_accepted=True).values_list('to_user_id', flat=True)
        fr_to = FriendRequest.objects.filter(to_user=user, is_accepted=True).values_list('from_user_id', flat=True)
        conflict_user_ids.update(fr_from)
        conflict_user_ids.update(fr_to)

    return conflict_user_ids

def select_juror_pool(dispute, pool_size=3, stake_amount=50):
    """
    Selects a pseudo-random pool of neutral jurors for a dispute.
    Filters out direct participants and their friends, and verifies minimum reward points balance.
    Locks stake_amount reward points for each selected juror in an atomic database transaction.
    """
    with transaction.atomic():
        # Conflict of interest check
        excluded_ids = get_conflict_user_ids(dispute)

        # Candidate pool query: active users with sufficient reward points, excluding conflict users
        candidates = User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=stake_amount
        ).exclude(id__in=excluded_ids).exclude(juror_assignments__dispute=dispute)

        candidate_list = list(candidates)

        if len(candidate_list) < pool_size:
            # Fallback handling
            jury_pool, _ = JuryPool.objects.get_or_create(
                dispute=dispute,
                defaults={'status': 'fallback'}
            )
            if jury_pool.status != 'fallback':
                jury_pool.status = 'fallback'
                jury_pool.save()
            return jury_pool, []

        # Pseudo-random selection
        selected_users = random.sample(candidate_list, pool_size)

        jury_pool, _ = JuryPool.objects.get_or_create(
            dispute=dispute,
            defaults={'status': 'active'}
        )
        jury_pool.status = 'active'
        jury_pool.save()

        assigned_jurors = []
        for juror_user in selected_users:
            profile = UserProfile.objects.select_for_update().get(user=juror_user)
            profile.rewards -= stake_amount
            profile.save()

            RewardLedger.objects.create(
                user=juror_user,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='juror_stake_lock',
                description=f"Reward points stake locked for juror assignment on dispute #{dispute.id}"
            )

            JurorAssignment.objects.create(
                dispute=dispute,
                jury_pool=jury_pool,
                juror=juror_user,
                stake_amount=stake_amount,
                voting_status='assigned',
                has_voted=False
            )
            assigned_jurors.append(juror_user)

            Notification.objects.create(
                recipient=juror_user,
                message=f"You have been selected as a juror for a dispute on task: '{dispute.task.title}'. {stake_amount} reward points locked as stake.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        dispute.status = 'voting'
        dispute.save()

        return jury_pool, assigned_jurors
