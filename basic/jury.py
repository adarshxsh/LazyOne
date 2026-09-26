import random
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Dispute, UserProfile, Friendship, FriendRequest, Notification


def select_juror_pool(dispute, panel_size=3):
    """
    Automatically selects a panel of neutral community jurors for a dispute.
    Filters out task participants (task poster, task assignee, dispute raiser)
    and their direct 1st-degree social ties (friends) using Django ORM queries
    before randomly selecting neutral community jurors.
    
    Selected jurors are assigned to dispute.jurors and notified immediately
    with direct links to the dispute detail page.
    """
    task = dispute.task
    excluded_user_ids = set()

    # 1. Collect task participants
    participants = []
    if task.posted_by_id:
        participants.append(task.posted_by)
        excluded_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        participants.append(task.taken_by)
        excluded_user_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        participants.append(dispute.raised_by)
        excluded_user_ids.add(dispute.raised_by_id)

    # 2. Collect direct 1st-degree friends of participants via ORM queries
    for p_user in participants:
        if hasattr(p_user, 'userprofile'):
            profile = p_user.userprofile
            # UserProfile.friends (forward M2M)
            excluded_user_ids.update(
                profile.friends.values_list('user_id', flat=True)
            )
            # UserProfile.friends (reverse M2M)
            excluded_user_ids.update(
                UserProfile.objects.filter(friends=profile).values_list('user_id', flat=True)
            )
            # Friendship model (from_user / to_user)
            excluded_user_ids.update(
                Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            )
            excluded_user_ids.update(
                Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            )

        # Accepted FriendRequests
        excluded_user_ids.update(
            FriendRequest.objects.filter(from_user=p_user, is_accepted=True).values_list('to_user_id', flat=True)
        )
        excluded_user_ids.update(
            FriendRequest.objects.filter(to_user=p_user, is_accepted=True).values_list('from_user_id', flat=True)
        )

    # 3. Query candidate active community users
    candidate_users = User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids)
    candidate_list = list(candidate_users)

    # 4. Randomly select neutral jurors
    num_to_select = min(panel_size, len(candidate_list))
    if num_to_select > 0:
        selected_jurors = random.sample(candidate_list, num_to_select)
    else:
        selected_jurors = []

    # 5. Assign selected jurors to the dispute
    dispute.jurors.set(selected_jurors)

    # 6. Notify selected community members
    dispute_link = reverse('dispute_detail', args=[dispute.id])
    for juror in selected_jurors:
        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a juror for dispute on task: '{task.title}'.",
            link=dispute_link
        )

    return selected_jurors
