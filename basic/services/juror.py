import random
from django.db import transaction
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import UserProfile, Friendship, JurorAssignment, Notification

def select_juror_pool(dispute, panel_size=3):
    """
    Selects an odd-numbered pool of neutral jurors for a dispute.
    Filters out task participants (posted_by, taken_by, raised_by) and their 1st-degree social graph ties
    from UserProfile.friends and Friendship model.
    Runs inside an atomic transaction.
    """
    with transaction.atomic():
        excluded_user_ids = set()
        task = dispute.task

        participants = [task.posted_by, task.taken_by, dispute.raised_by]
        for p in participants:
            if p:
                excluded_user_ids.add(p.id)

        # Gather 1st-degree friends of participants
        for p in participants:
            if not p:
                continue
            try:
                profile = p.userprofile
            except UserProfile.DoesNotExist:
                continue

            # 1. From UserProfile.friends ManyToMany
            for friend_profile in profile.friends.all():
                excluded_user_ids.add(friend_profile.user.id)

            # 2. From Friendship model (both directions)
            friendships_from = Friendship.objects.filter(from_user=profile).select_related('to_user__user')
            for f in friendships_from:
                excluded_user_ids.add(f.to_user.user.id)

            friendships_to = Friendship.objects.filter(to_user=profile).select_related('from_user__user')
            for f in friendships_to:
                excluded_user_ids.add(f.from_user.user.id)

        # Query eligible active users
        eligible_users = list(User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids))

        # Determine sampling count (must be odd)
        target_count = panel_size if panel_size % 2 == 1 else panel_size + 1
        num_candidates = len(eligible_users)

        if num_candidates < target_count:
            # Adjust to largest available odd number
            target_count = num_candidates if num_candidates % 2 == 1 else num_candidates - 1

        if target_count <= 0:
            return []

        selected_jurors = random.sample(eligible_users, target_count)

        assignments = []
        for juror in selected_jurors:
            assignment, created = JurorAssignment.objects.get_or_create(dispute=dispute, juror=juror)
            assignments.append(assignment)
            if created:
                Notification.objects.create(
                    recipient=juror,
                    message=f"You have been assigned as a juror for dispute on task: '{task.title}'",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        return selected_jurors
