import random
from django.db import transaction
from django.contrib.auth.models import User
from django.urls import reverse
from .models import JurorAssignment, RewardLedger, Notification, UserProfile

def get_excluded_user_ids(dispute):
    task = dispute.task
    excluded_ids = set()

    # Task counterparties and dispute raiser
    if task.posted_by_id:
        excluded_ids.add(task.posted_by_id)
    if task.taken_by_id:
        excluded_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        excluded_ids.add(dispute.raised_by_id)

    users_to_check = []
    if task.posted_by:
        users_to_check.append(task.posted_by)
    if task.taken_by:
        users_to_check.append(task.taken_by)
    if dispute.raised_by:
        users_to_check.append(dispute.raised_by)

    for user in users_to_check:
        try:
            profile = user.userprofile
            for friend_profile in profile.friends.all():
                excluded_ids.add(friend_profile.user_id)
            for friend_profile in UserProfile.objects.filter(friends=profile):
                excluded_ids.add(friend_profile.user_id)
        except UserProfile.DoesNotExist:
            pass

    return excluded_ids

def select_juror_pool(dispute, stake_amount=50):
    """
    Returns QuerySet of eligible candidate users for juror selection.
    Excludes counterparties, raiser, and all their direct friends.
    Ensures rewards balance >= stake_amount.
    """
    excluded_ids = get_excluded_user_ids(dispute)
    candidates = User.objects.filter(
        is_active=True
    ).exclude(
        id__in=excluded_ids
    ).filter(
        userprofile__rewards__gte=stake_amount
    ).distinct()
    return candidates

def select_and_assign_jurors(dispute, stake_amount=50, required_count=3):
    """
    Selects neutral jurors, locks required stake from each, creates JurorAssignment records,
    and logs RewardLedger entries.
    If fewer than required_count candidates exist, puts dispute in 'pending_jurors' state.
    """
    candidates = select_juror_pool(dispute, stake_amount=stake_amount)
    
    if candidates.count() < required_count:
        dispute.status = 'pending_jurors'
        dispute.save()
        return []

    candidate_list = list(candidates)
    selected_users = random.sample(candidate_list, required_count)
    assignments = []

    for user in selected_users:
        profile = user.userprofile
        if profile.rewards < stake_amount:
            continue
        
        profile.rewards -= stake_amount
        profile.save()

        assignment = JurorAssignment.objects.create(
            dispute=dispute,
            juror=user,
            stake_amount=stake_amount,
            status='assigned'
        )
        assignments.append(assignment)

        RewardLedger.objects.create(
            user=user,
            task=dispute.task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake locked for dispute on task: '{dispute.task.title}'"
        )

        Notification.objects.create(
            recipient=user,
            message=f"You have been selected as a juror for dispute on task: '{dispute.task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    if len(assignments) == required_count:
        dispute.status = 'open'
        dispute.save()

    return assignments
