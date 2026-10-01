import random
from django.contrib.auth.models import User
from django.urls import reverse
from .models import UserProfile, Dispute, DisputeJuror, Friendship, Notification

def get_social_distance_excluded_user_ids(dispute):
    """
    Returns user IDs of task poster, taker, raiser, and their 1st- and 2nd-degree friends.
    """
    task = dispute.task
    parties = set()
    if task.posted_by:
        parties.add(task.posted_by.id)
    if task.taken_by:
        parties.add(task.taken_by.id)
    if dispute.raised_by:
        parties.add(dispute.raised_by.id)

    def get_direct_friends_user_ids(user_ids_set):
        direct_friends = set()
        for uid in user_ids_set:
            try:
                up = UserProfile.objects.get(user_id=uid)
            except UserProfile.DoesNotExist:
                continue
            # 1. UserProfile.friends M2M
            direct_friends.update(up.friends.values_list('user_id', flat=True))
            # 2. Reverse UserProfile.friends M2M
            direct_friends.update(UserProfile.objects.filter(friends=up).values_list('user_id', flat=True))
            # 3. Friendship model (from_user)
            direct_friends.update(Friendship.objects.filter(from_user=up).values_list('to_user__user_id', flat=True))
            # 4. Friendship model (to_user)
            direct_friends.update(Friendship.objects.filter(to_user=up).values_list('from_user__user_id', flat=True))
        return direct_friends

    first_degree = get_direct_friends_user_ids(parties) - parties
    second_degree = get_direct_friends_user_ids(first_degree) - parties - first_degree

    return parties | first_degree | second_degree


def select_jurors_for_dispute(dispute, count=3):
    """
    Filters candidate users excluding task parties and 1st/2nd degree friends,
    users in open disputes, and users with low reward balance.
    Picks `count` active users using a weighted random draw based on activity.
    Creates DisputeJuror records and sends notifications.
    """
    excluded_user_ids = get_social_distance_excluded_user_ids(dispute)

    # Exclude users with open disputes
    open_disputes = Dispute.objects.filter(status='open')
    users_in_open_disputes = set(open_disputes.values_list('raised_by_id', flat=True))
    users_in_open_disputes.update(open_disputes.values_list('task__posted_by_id', flat=True))
    users_in_open_disputes.update(open_disputes.values_list('task__taken_by_id', flat=True))
    users_in_open_disputes.update(DisputeJuror.objects.filter(dispute__status='open').values_list('user_id', flat=True))

    excluded_user_ids.update(users_in_open_disputes)

    # Filter active candidate users with positive reward balance
    candidates_qs = User.objects.filter(
        is_active=True,
        userprofile__rewards__gt=0
    ).exclude(id__in=excluded_user_ids)

    candidate_list = list(candidates_qs)
    if not candidate_list:
        return []

    # Compute activity weights for candidates
    weights = []
    for user in candidate_list:
        activity = (
            1 +
            user.posted_tasks.count() +
            user.taken_tasks.count() +
            user.sent_messages.count()
        )
        weights.append(activity)

    # Weighted random draw without replacement
    selected_jurors = []
    pool = list(zip(candidate_list, weights))
    target_count = min(count, len(pool))

    while len(selected_jurors) < target_count and pool:
        total_weight = sum(w for _, w in pool)
        if total_weight <= 0:
            chosen = random.choice([u for u, _ in pool])
        else:
            r = random.uniform(0, total_weight)
            cum = 0
            chosen = None
            for u, w in pool:
                if cum + w >= r:
                    chosen = u
                    break
                cum += w
            if chosen is None:
                chosen = pool[-1][0]

        selected_jurors.append(chosen)
        pool = [(u, w) for u, w in pool if u != chosen]

    # Create DisputeJuror records and notifications
    created_records = []
    for juror in selected_jurors:
        dj, created = DisputeJuror.objects.get_or_create(dispute=dispute, user=juror)
        created_records.append(dj)
        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a juror for dispute on task: '{dispute.task.title}'",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return created_records
