import logging
from django.contrib.auth.models import User
from django.conf import settings
from django.urls import reverse
from basic.models import UserProfile, Friendship, Task, Notification

logger = logging.getLogger(__name__)

STAKE_THRESHOLD = getattr(settings, 'JUROR_STAKE_THRESHOLD', 50)


class InsufficientJurorsError(ValueError):
    """Exception raised when there are not enough eligible neutral jurors in the pool."""
    pass


def get_excluded_user_ids(dispute):
    """
    Constructs a set of user IDs to exclude from juror selection for a given dispute.
    
    Excludes:
    - Task poster (task.posted_by)
    - Task worker (task.taken_by)
    - Dispute raiser (dispute.raised_by)
    - Direct friends of poster and worker (checked in UserProfile.friends and Friendship bidirectionally)
    - Active task partners (users engaged in 'in_progress' or 'disputed' tasks with poster or worker)
    """
    excluded_ids = set()

    task = dispute.task if dispute else None
    poster = task.posted_by if task else None
    worker = task.taken_by if task else None
    raised_by = dispute.raised_by if dispute else None

    counterparties = [u for u in [poster, worker, raised_by] if u is not None]

    # 1. Exclude counterparties directly
    for c in counterparties:
        excluded_ids.add(c.id)

    # 2. Exclude direct friends of counterparties (bidirectional across UserProfile.friends and Friendship)
    for c in counterparties:
        try:
            profile = c.userprofile
        except (UserProfile.DoesNotExist, AttributeError):
            profile = UserProfile.objects.filter(user=c).first()

        if profile:
            # UserProfile.friends (M2M) - forward
            forward_friends = profile.friends.values_list('user_id', flat=True)
            excluded_ids.update(forward_friends)

            # UserProfile.friends (M2M) - reverse
            reverse_friends = UserProfile.objects.filter(friends=profile).values_list('user_id', flat=True)
            excluded_ids.update(reverse_friends)

            # Friendship model - from_user -> to_user
            friendship_to = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            excluded_ids.update(friendship_to)

            # Friendship model - to_user -> from_user
            friendship_from = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            excluded_ids.update(friendship_from)

    # 3. Exclude active task partners currently engaged in 'in_progress' or 'disputed' tasks with either counterparty
    active_statuses = ['in_progress', 'disputed']
    for c in counterparties:
        # Tasks posted by c that have an active taker/worker
        taken_task_partners = Task.objects.filter(
            posted_by=c,
            status__in=active_statuses
        ).exclude(taken_by=None).values_list('taken_by_id', flat=True)
        excluded_ids.update(taken_task_partners)

        # Tasks taken by c that have a poster
        posted_task_partners = Task.objects.filter(
            taken_by=c,
            status__in=active_statuses
        ).values_list('posted_by_id', flat=True)
        excluded_ids.update(posted_task_partners)

    return excluded_ids


def select_jurors_for_dispute(dispute, panel_size=3):
    """
    Selects an odd-numbered panel of neutral, non-conflicted eligible jurors for a dispute.
    
    Parameters:
        dispute (Dispute): The dispute instance requiring juror selection.
        panel_size (int): The number of jurors to select (must be positive and odd, default 3).
        
    Returns:
        List[User]: List of selected User objects.
        
    Raises:
        ValueError: If panel_size is not a positive odd integer.
        InsufficientJurorsError: If fewer than panel_size non-conflicted eligible candidates exist.
    """
    if panel_size <= 0 or panel_size % 2 == 0:
        raise ValueError(f"panel_size must be a positive odd number, got {panel_size}.")

    excluded_ids = get_excluded_user_ids(dispute)

    logger.info(
        "Initiating juror selection for dispute ID %s (Task ID: %s, panel_size: %d). Total excluded user IDs: %d",
        dispute.id, dispute.task.id if dispute and dispute.task else None, panel_size, len(excluded_ids)
    )

    # Filter candidates: user active, userprofile rewards >= STAKE_THRESHOLD, not in excluded_ids
    candidate_qs = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=STAKE_THRESHOLD
    ).exclude(
        id__in=excluded_ids
    ).order_by('?')

    jurors = list(candidate_qs[:panel_size])

    if len(jurors) < panel_size:
        error_msg = (
            f"Fewer than {panel_size} eligible neutral candidates found for dispute {dispute.id if dispute else 'N/A'}. "
            f"Required: {panel_size}, Available: {len(jurors)}."
        )
        logger.error(error_msg)
        raise InsufficientJurorsError(error_msg)

    logger.info(
        "Successfully selected %d jurors for dispute ID %s: %s",
        len(jurors), dispute.id if dispute else 'N/A', [j.username for j in jurors]
    )

    # Issue summons notifications to selected jurors
    for juror in jurors:
        try:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been summoned as a juror for dispute on task: '{dispute.task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        except Exception as e:
            logger.warning("Failed to create summons notification for juror %s: %s", juror.username, e)

    return jurors
