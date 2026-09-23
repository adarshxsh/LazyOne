import random
from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from .models import JuryPanel, JuryMember, Notification

def get_exclusion_user_ids(dispute):
    exclusion_ids = set()
    task = dispute.task

    # 1. Task Poster
    if task.posted_by_id:
        exclusion_ids.add(task.posted_by_id)
        if hasattr(task.posted_by, 'userprofile'):
            friends_user_ids = task.posted_by.userprofile.friends.values_list('user_id', flat=True)
            exclusion_ids.update(friends_user_ids)

    # 2. Task Taker
    if task.taken_by_id:
        exclusion_ids.add(task.taken_by_id)
        if hasattr(task.taken_by, 'userprofile'):
            friends_user_ids = task.taken_by.userprofile.friends.values_list('user_id', flat=True)
            exclusion_ids.update(friends_user_ids)

    # 3. Dispute Raiser
    if dispute.raised_by_id:
        exclusion_ids.add(dispute.raised_by_id)
        if hasattr(dispute.raised_by, 'userprofile'):
            friends_user_ids = dispute.raised_by.userprofile.friends.values_list('user_id', flat=True)
            exclusion_ids.update(friends_user_ids)

    return exclusion_ids

def select_jury_panel(dispute, panel_size=None):
    if panel_size is None:
        panel_size = getattr(settings, 'JURY_PANEL_SIZE', 3)

    exclusion_ids = get_exclusion_user_ids(dispute)

    candidates = list(
        User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=100
        ).exclude(
            id__in=exclusion_ids
        ).distinct()
    )

    panel, created = JuryPanel.objects.get_or_create(
        dispute=dispute,
        defaults={'status': 'assigned'}
    )

    if len(candidates) < panel_size:
        panel.status = 'fallback'
        panel.save()
        panel.members.all().delete()
        return panel

    selected_users = random.sample(candidates, panel_size)

    panel.status = 'assigned'
    panel.save()
    panel.members.all().delete()

    dispute_url = reverse('dispute_detail', args=[dispute.id])

    for juror_user in selected_users:
        JuryMember.objects.create(
            panel=panel,
            user=juror_user,
            status='assigned'
        )
        Notification.objects.create(
            recipient=juror_user,
            message=f"You have been selected as a juror for dispute on task: '{dispute.task.title}'.",
            link=dispute_url
        )

    return panel
