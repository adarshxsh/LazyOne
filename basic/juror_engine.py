import random
from django.contrib.auth.models import User
from django.urls import reverse
from django.db import transaction
from .models import Dispute, DisputeJuror, UserProfile, Friendship, Notification

def get_direct_friend_user_ids(user):
    """Returns a set of user IDs for all first-degree friends of `user`."""
    if not user:
        return set()
    
    friend_user_ids = set()
    try:
        profile = user.userprofile
    except UserProfile.DoesNotExist:
        return friend_user_ids

    # Query UserProfile.friends ManyToMany
    friend_user_ids.update(profile.friends.values_list('user_id', flat=True))

    # Query Friendship table (both directions)
    friend_user_ids.update(
        Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
    )
    friend_user_ids.update(
        Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
    )

    return friend_user_ids

def select_and_assign_jurors(dispute, min_panel_size=3, target_panel_size=5):
    """
    Evaluates candidate eligibility at dispute creation, filtering out task participants,
    users in active disputes, direct social connections, and cohort peers before sampling
    a juror panel of 3 to 5 eligible community members.
    
    If candidate pool falls below min_panel_size (3), flags dispute for staff intervention.
    """
    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by
    raised_by = dispute.raised_by

    # Clear previous juror assignments for this dispute if re-evaluating
    DisputeJuror.objects.filter(dispute=dispute).delete()

    # 1. Participant Exclusions
    participant_ids = {u.id for u in [poster, taker, raised_by] if u is not None}

    # 2. Active Dispute Exclusions (users involved in open/unresolved disputes)
    open_disputes = Dispute.objects.filter(status='open').exclude(id=dispute.id)
    active_dispute_user_ids = set()
    
    for open_disp in open_disputes:
        if open_disp.raised_by_id:
            active_dispute_user_ids.add(open_disp.raised_by_id)
        if open_disp.task:
            if open_disp.task.posted_by_id:
                active_dispute_user_ids.add(open_disp.task.posted_by_id)
            if open_disp.task.taken_by_id:
                active_dispute_user_ids.add(open_disp.task.taken_by_id)
        
    active_juror_ids = set(
        DisputeJuror.objects.filter(dispute__status='open').exclude(dispute_id=dispute.id).values_list('user_id', flat=True)
    )
    active_dispute_user_ids.update(active_juror_ids)

    # 3. Direct Social Ties (First-Degree Friends & Mutual Connections)
    poster_friends = get_direct_friend_user_ids(poster)
    taker_friends = get_direct_friend_user_ids(taker)
    social_connection_ids = poster_friends | taker_friends

    # Combine Hard Exclusions
    hard_exclude_ids = participant_ids | active_dispute_user_ids | social_connection_ids

    # Query hard-eligible candidate users (must be active users not in hard_exclude_ids)
    eligible_candidates = list(User.objects.filter(is_active=True).exclude(id__in=hard_exclude_ids))

    # 4. Cohort Peers Exclusion (Batch & Hostel attributes)
    poster_profile = getattr(poster, 'userprofile', None)
    taker_profile = getattr(taker, 'userprofile', None)

    non_peer_candidates = []
    peer_candidates = []

    for candidate in eligible_candidates:
        c_profile = getattr(candidate, 'userprofile', None)
        is_peer = False
        if c_profile:
            # Check against poster cohort
            if poster_profile and poster_profile.hostel and c_profile.hostel:
                if c_profile.hostel == poster_profile.hostel and c_profile.batch == poster_profile.batch:
                    is_peer = True
            # Check against taker cohort
            if taker_profile and taker_profile.hostel and c_profile.hostel:
                if c_profile.hostel == taker_profile.hostel and c_profile.batch == taker_profile.batch:
                    is_peer = True

        if is_peer:
            peer_candidates.append(candidate)
        else:
            non_peer_candidates.append(candidate)

    # Shuffle candidate lists to maintain randomness
    random.shuffle(non_peer_candidates)
    random.shuffle(peer_candidates)

    # Determine selected panel
    selected_jurors = []

    if len(non_peer_candidates) >= min_panel_size:
        # Adequate non-peer pool available! Exclude cohort peers completely.
        k = min(target_panel_size, len(non_peer_candidates))
        selected_jurors = non_peer_candidates[:k]
    else:
        # Non-peer pool is inadequate (< min_panel_size). Take all non-peers and supplement with cohort peers.
        selected_jurors = list(non_peer_candidates)
        needed = target_panel_size - len(selected_jurors)
        selected_jurors.extend(peer_candidates[:needed])

    # 5. Check if panel size meets minimum requirement
    if len(selected_jurors) < min_panel_size:
        dispute.flagged_for_staff = True
    else:
        dispute.flagged_for_staff = False
    dispute.save()

    # 6. Persist Assignments and Dispatch Notifications
    with transaction.atomic():
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for juror_user in selected_jurors:
            DisputeJuror.objects.create(dispute=dispute, user=juror_user)
            Notification.objects.create(
                recipient=juror_user,
                message=f"You have been selected as a community juror for dispute on task: '{task.title}'.",
                link=dispute_link
            )

    return selected_jurors
