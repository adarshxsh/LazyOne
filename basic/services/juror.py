import random
from django.contrib.auth.models import User
from django.urls import reverse
from ..models import Dispute, JurorAssignment, UserProfile, Friendship, Notification


def get_conflicted_user_ids(dispute):
    """
    Returns a set of user IDs that are conflicted for the given dispute.
    Excludes:
    - Task participants: posted_by, taken_by, raised_by
    - Direct and reverse friends in UserProfile.friends
    - High closeness score connections (>30) in Friendship records
    """
    task = dispute.task
    participant_user_ids = set()
    if task.posted_by_id:
        participant_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        participant_user_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        participant_user_ids.add(dispute.raised_by_id)

    conflicted_user_ids = set(participant_user_ids)

    participant_profiles = UserProfile.objects.filter(user_id__in=participant_user_ids)
    participant_profile_ids = set(participant_profiles.values_list('id', flat=True))

    if not participant_profile_ids:
        return conflicted_user_ids

    # 1. UserProfile.friends exclusions (direct M2M from participant profiles)
    for prof in participant_profiles:
        friend_user_ids = prof.friends.values_list('user_id', flat=True)
        conflicted_user_ids.update(friend_user_ids)

    # Reverse M2M: profiles that list participant profiles in their friends list
    reverse_friend_user_ids = UserProfile.objects.filter(
        friends__id__in=participant_profile_ids
    ).values_list('user_id', flat=True)
    conflicted_user_ids.update(reverse_friend_user_ids)

    # 2. Friendship records with closeness > 30
    high_closeness_from = Friendship.objects.filter(
        from_user_id__in=participant_profile_ids,
        closeness__gt=30
    ).values_list('to_user__user_id', flat=True)
    conflicted_user_ids.update(high_closeness_from)

    high_closeness_to = Friendship.objects.filter(
        to_user_id__in=participant_profile_ids,
        closeness__gt=30
    ).values_list('from_user__user_id', flat=True)
    conflicted_user_ids.update(high_closeness_to)

    return conflicted_user_ids


def assign_jurors_for_dispute(dispute):
    """
    Samples 3 to 5 eligible non-conflicted users upon dispute creation,
    creates JurorAssignment records, and sends notifications.
    """
    conflicted_user_ids = get_conflicted_user_ids(dispute)

    # Eligible non-conflicted active users
    candidates = list(User.objects.filter(is_active=True).exclude(id__in=conflicted_user_ids))

    if not candidates:
        return []

    # Sample 3 to 5 users if available
    if len(candidates) >= 5:
        sample_size = random.randint(3, 5)
    elif len(candidates) >= 3:
        sample_size = len(candidates)
    else:
        sample_size = len(candidates)

    selected_users = random.sample(candidates, sample_size)

    # Clear previous assignments if re-opened dispute
    dispute.juror_assignments.all().delete()

    assignments = []
    task_title = dispute.task.title
    dispute_link = reverse('dispute_detail', args=[dispute.id])

    for user in selected_users:
        assignment = JurorAssignment.objects.create(
            dispute=dispute,
            user=user,
            status='assigned'
        )
        assignments.append(assignment)

        Notification.objects.create(
            recipient=user,
            message=f"You have been assigned as a juror for dispute on task: '{task_title}'.",
            link=dispute_link
        )

    return assignments
