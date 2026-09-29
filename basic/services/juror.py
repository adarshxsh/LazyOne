import random
from django.contrib.auth.models import User
from django.urls import reverse
from ..models import JurorAssignment, UserProfile, Notification


def select_juror_pool(dispute):
    """
    Selects a panel of 3 active, non-staff, neutral community jurors for a dispute,
    filtering out 1st-degree friends of the task poster and task taker.
    """
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by
    raised_by = dispute.raised_by

    excluded_user_ids = set()

    # Exclude task poster, taker, dispute raiser
    if posted_by:
        excluded_user_ids.add(posted_by.id)
    if taken_by:
        excluded_user_ids.add(taken_by.id)
    if raised_by:
        excluded_user_ids.add(raised_by.id)

    # Exclude 1st-degree friends of task poster
    if posted_by and hasattr(posted_by, 'userprofile'):
        poster_profile = posted_by.userprofile
        excluded_user_ids.update(poster_profile.friends.values_list('user_id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friends=poster_profile).values_list('user_id', flat=True))

    # Exclude 1st-degree friends of task taker
    if taken_by and hasattr(taken_by, 'userprofile'):
        taker_profile = taken_by.userprofile
        excluded_user_ids.update(taker_profile.friends.values_list('user_id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friends=taker_profile).values_list('user_id', flat=True))

    # Query active, non-staff candidate users
    candidates = User.objects.filter(is_active=True, is_staff=False).exclude(id__in=excluded_user_ids)
    candidate_ids = list(candidates.values_list('id', flat=True))

    if len(candidate_ids) >= 3:
        selected_ids = random.sample(candidate_ids, 3)
        dispute.is_staff_escalated = False
    else:
        selected_ids = candidate_ids
        dispute.is_staff_escalated = True

    dispute.save()

    # Clear any existing assignments if re-opened/re-assigned
    dispute.juror_assignments.all().delete()

    assigned_jurors = []
    # Create JurorAssignment records & notifications for selected jurors
    for juror_id in selected_ids:
        juror = User.objects.get(id=juror_id)
        assignment = JurorAssignment.objects.create(dispute=dispute, juror=juror)
        assigned_jurors.append(assignment)

        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a juror for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    # If flagged for staff escalation, notify staff members
    if dispute.is_staff_escalated:
        staff_users = User.objects.filter(is_staff=True, is_active=True)
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"Dispute on task '{task.title}' has under 3 conflict-free jurors and requires staff escalation.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return assigned_jurors
