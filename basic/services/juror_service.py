from django.db import models
from django.contrib.auth.models import User
from django.urls import reverse
from ..models import Dispute, JurorAssignment, UserProfile, Friendship, Notification

def assign_jurors_for_dispute(dispute, count=3):
    """
    Selects up to `count` neutral community members as jurors for a dispute,
    excluding dispute participants, direct friends, and high-closeness connections (closeness >= 50).
    Creates JurorAssignment records and sends notifications to selected jurors.
    """
    # Clear pre-existing assignments for this dispute if any
    dispute.juror_assignments.all().delete()

    task = dispute.task
    participants = set()
    if task.posted_by_id:
        participants.add(task.posted_by)
    if task.taken_by_id:
        participants.add(task.taken_by)
    if dispute.raised_by_id:
        participants.add(dispute.raised_by)

    excluded_user_ids = {p.id for p in participants}

    participant_profiles = []
    for p in participants:
        if hasattr(p, 'userprofile') and p.userprofile:
            participant_profiles.append(p.userprofile)

    if participant_profiles:
        # Exclude direct friends from UserProfile.friends
        for profile in participant_profiles:
            friend_user_ids = profile.friends.values_list('user_id', flat=True)
            excluded_user_ids.update(friend_user_ids)

        # Exclude users with Friendship closeness >= 50 relative to any participant
        close_friendships = Friendship.objects.filter(
            models.Q(from_user__in=participant_profiles, closeness__gte=50) |
            models.Q(to_user__in=participant_profiles, closeness__gte=50)
        )
        for fs in close_friendships:
            if fs.from_user and fs.from_user.user_id:
                excluded_user_ids.add(fs.from_user.user_id)
            if fs.to_user and fs.to_user.user_id:
                excluded_user_ids.add(fs.to_user.user_id)

    # Query active candidate users excluding excluded_user_ids
    candidate_users = User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids)

    selected_jurors = list(candidate_users.order_by('?')[:count])

    assignments = []
    notifications = []
    dispute_link = reverse('dispute_detail', args=[dispute.id])

    for juror in selected_jurors:
        assignment = JurorAssignment.objects.create(dispute=dispute, juror=juror)
        assignments.append(assignment)

        notifications.append(
            Notification(
                recipient=juror,
                message=f"You have been assigned as a juror for the dispute on task: '{task.title}'.",
                link=dispute_link
            )
        )

    if notifications:
        Notification.objects.bulk_create(notifications)

    return assignments
