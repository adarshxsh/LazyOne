import random
from django.db import transaction
from django.urls import reverse
from django.contrib.auth.models import User
from .models import Dispute, JurorAssignment, RewardLedger, Notification, DisputeAuditEvent

def select_jurors_for_dispute(dispute, panel_size=3, tier=1, stake_amount=None):
    if stake_amount is None:
        if tier == 1:
            stake_amount = 50
        else:
            stake_amount = max(100, dispute.deposit_amount * 2)

    task = dispute.task
    excluded_user_ids = set()

    # Exclude task participants and dispute raiser
    if task.posted_by_id:
        excluded_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        excluded_user_ids.add(task.taken_by_id)
    if dispute.raised_by_id:
        excluded_user_ids.add(dispute.raised_by_id)

    # Exclude friends of posted_by and taken_by
    if hasattr(task.posted_by, 'userprofile'):
        friends = task.posted_by.userprofile.friends.values_list('user_id', flat=True)
        excluded_user_ids.update(friends)
    if task.taken_by and hasattr(task.taken_by, 'userprofile'):
        friends = task.taken_by.userprofile.friends.values_list('user_id', flat=True)
        excluded_user_ids.update(friends)

    # For Tier 2 (Appeal Tier), EXCLUDE all Tier 1 assigned jurors
    if tier == 2:
        tier1_juror_ids = dispute.juror_assignments.filter(tier=1).values_list('juror_id', flat=True)
        excluded_user_ids.update(tier1_juror_ids)

    # Filter candidate pool
    candidates = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=stake_amount
    ).exclude(id__in=excluded_user_ids)

    candidate_list = list(candidates)
    if len(candidate_list) < panel_size:
        selected_users = candidate_list
    else:
        selected_users = random.sample(candidate_list, panel_size)

    assignments = []
    with transaction.atomic():
        for user in selected_users:
            profile = user.userprofile
            profile.rewards -= stake_amount
            profile.save()

            RewardLedger.objects.create(
                user=user,
                task=task,
                amount=-stake_amount,
                transaction_type='juror_stake_lock',
                description=f"Juror stake bond locked for dispute on task: '{task.title}' (Tier {tier})"
            )

            assignment, created = JurorAssignment.objects.get_or_create(
                dispute=dispute,
                juror=user,
                defaults={
                    'tier': tier,
                    'stake_amount': stake_amount,
                }
            )
            assignments.append(assignment)

            Notification.objects.create(
                recipient=user,
                message=f"You have been selected as a juror for dispute on task '{task.title}' (Tier {tier}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        if assignments:
            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=None,
                event_type='jurors_assigned',
                details_json={
                    'tier': tier,
                    'juror_ids': [a.juror_id for a in assignments],
                    'stake_amount': stake_amount
                }
            )

    return assignments
