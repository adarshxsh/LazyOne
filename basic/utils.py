import random
from django.contrib.auth.models import User
from django.urls import reverse
from django.db import models
from .models import UserProfile, Friendship, JuryPool, JurorAssignment, Notification


def select_juror_pool(dispute, num_jurors=3):
    """
    ORM-based conflict-of-interest isolation algorithm for juror selection.
    Excludes direct task parties, 1st degree friends, 2nd degree friends,
    and high closeness connections (> 70) in Friendship.
    Falls back gracefully by relaxing 2nd-degree friend exclusions if candidate pool is small.
    """
    task = dispute.task

    # 1. Direct parties
    direct_party_users = set()
    if task.posted_by:
        direct_party_users.add(task.posted_by)
    if task.taken_by:
        direct_party_users.add(task.taken_by)
    if dispute.raised_by:
        direct_party_users.add(dispute.raised_by)

    direct_party_user_ids = {u.id for u in direct_party_users}

    # Direct party profiles
    party_profiles = UserProfile.objects.filter(user_id__in=direct_party_user_ids)

    # 2. 1st degree friends
    first_degree_profile_ids = set()
    for profile in party_profiles:
        first_degree_profile_ids.update(profile.friends.values_list('id', flat=True))
        first_degree_profile_ids.update(UserProfile.objects.filter(friends=profile).values_list('id', flat=True))

    # Remove direct parties from 1st degree friend profiles if present
    first_degree_profile_ids.difference_update(party_profiles.values_list('id', flat=True))

    # 3. High closeness connections in Friendship (closeness > 70)
    high_close_friendships = Friendship.objects.filter(
        closeness__gt=70
    ).filter(
        models.Q(from_user__in=party_profiles) | models.Q(to_user__in=party_profiles)
    )

    high_close_profile_ids = set()
    for fs in high_close_friendships:
        high_close_profile_ids.add(fs.from_user_id)
        high_close_profile_ids.add(fs.to_user_id)

    # 4. 2nd degree friends (friends of 1st degree friends)
    first_degree_profiles = UserProfile.objects.filter(id__in=first_degree_profile_ids)
    second_degree_profile_ids = set()
    for profile in first_degree_profiles:
        second_degree_profile_ids.update(profile.friends.values_list('id', flat=True))
        second_degree_profile_ids.update(UserProfile.objects.filter(friends=profile).values_list('id', flat=True))

    # 5. Strict exclusions (Direct parties, 1st degree friends, closeness > 70)
    strict_excluded_user_ids = set(direct_party_user_ids)

    if first_degree_profile_ids:
        strict_excluded_user_ids.update(
            User.objects.filter(userprofile__id__in=first_degree_profile_ids).values_list('id', flat=True)
        )
    if high_close_profile_ids:
        strict_excluded_user_ids.update(
            User.objects.filter(userprofile__id__in=high_close_profile_ids).values_list('id', flat=True)
        )

    # Full exclusions (including 2nd degree friends)
    full_excluded_user_ids = set(strict_excluded_user_ids)
    if second_degree_profile_ids:
        full_excluded_user_ids.update(
            User.objects.filter(userprofile__id__in=second_degree_profile_ids).values_list('id', flat=True)
        )

    # Query candidate users
    candidates = list(User.objects.filter(is_active=True).exclude(id__in=full_excluded_user_ids))

    # Fallback: if candidate pool is smaller than required, relax 2nd degree friend exclusion
    if len(candidates) < num_jurors:
        candidates = list(User.objects.filter(is_active=True).exclude(id__in=strict_excluded_user_ids))

    # Pseudo-random sampling
    num_to_select = min(num_jurors, len(candidates))
    selected_jurors = random.sample(candidates, num_to_select) if candidates else []

    # Get or create JuryPool
    jury_pool, _ = JuryPool.objects.get_or_create(dispute=dispute)

    # Reset old assignments for this pool/dispute
    JurorAssignment.objects.filter(dispute=dispute).delete()

    dispute_url = reverse('dispute_detail', args=[dispute.id])

    # Assign selected jurors and notify them
    assignments = []
    for juror in selected_jurors:
        assignment = JurorAssignment.objects.create(
            dispute=dispute,
            jury_pool=jury_pool,
            juror=juror,
            voting_status='pending',
            has_voted=False
        )
        assignments.append(assignment)

        Notification.objects.create(
            recipient=juror,
            message=f"You have been selected as a juror for the dispute on task: '{task.title}'.",
            link=dispute_url
        )

    return jury_pool
