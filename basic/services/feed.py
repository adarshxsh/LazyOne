"""
Wave distribution - LazyOne's core idea. Full design, costs and tuning:
docs/Ideation/WAVE_DISTRIBUTION.md

A task reaches the poster's closest friends first and spreads outward:

    wave 0  friends the poster rates closeness >= 80
    wave 1  friends rated >= 40                       (standard: +10 min)
    wave 2  all friends + friends-of-friends          (standard: +30 min)
    wave 3  everyone in the community                 (standard: +2 h)

Adaptive schedule: when a task opens, waves with nobody in them are skipped, so a poster
with no close friends doesn't make everyone wait. The resulting start times are stored on
the task (opened_at, wave1_at, wave2_at, wave3_at; always non-decreasing).

Feed query: the viewer's circle (who rated them how, and friends-of-friends) is loaded once
and cached, then a single indexed query keeps tasks whose wave for this viewer has started.
"""
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from outbox.services import emit

from ..models import Friendship, Task, UserProfile

LAST_WAVE = 3
CIRCLE_TTL = 60  # seconds
_CIRCLE_KEY = "wave_circle:v1:{}"


def _cfg():
    return settings.LAZYONE


def wave_for_closeness(closeness):
    c = _cfg()
    if closeness is None:
        return LAST_WAVE
    if closeness >= c['CLOSENESS_WAVE0']:
        return 0
    if closeness >= c['CLOSENESS_WAVE1']:
        return 1
    return 2


# ---------------------------------------------------------------- schedule (write side)

def _poster_wave_sizes(poster):
    """How many people are in waves 0-2 for this poster (wave 3 = community, always non-empty)."""
    rows = list(Friendship.objects.filter(from_user__user=poster).values_list('to_user_id', 'closeness'))
    sizes = [0, 0, 0]
    for _, closeness in rows:
        sizes[wave_for_closeness(closeness)] += 1
    if not sizes[2] and rows:
        friend_ids = [pid for pid, _ in rows]
        has_fof = (Friendship.objects.filter(from_user_id__in=friend_ids)
                   .exclude(to_user__user=poster).exclude(to_user_id__in=friend_ids).exists())
        sizes[2] = 1 if has_fof else 0
    return sizes


def wave_schedule(poster, policy, opened_at):
    """
    Start time of waves 1-3 for a task opened now. Empty waves take zero time: each non-empty
    wave starts after the previous non-empty one plus its own standard gap.
    Returns {'wave1_at', 'wave2_at', 'wave3_at'}; wave3_at is None for friends_only.
    """
    if policy == Task.WavePolicy.COMMUNITY_NOW:
        return {'wave1_at': opened_at, 'wave2_at': opened_at, 'wave3_at': opened_at}

    delays = _cfg()['WAVE_DELAYS']
    sizes = _poster_wave_sizes(poster)
    non_empty = [sizes[0] > 0, sizes[1] > 0, sizes[2] > 0, policy != Task.WavePolicy.FRIENDS_ONLY]

    starts = [None] * 4
    t = None
    for k in range(4):
        if not non_empty[k]:
            continue
        t = opened_at if t is None else t + (delays[k] - delays[k - 1])
        starts[k] = t
    # An empty wave opens together with the next non-empty one (keeps times non-decreasing).
    for k in range(LAST_WAVE - 1, -1, -1):
        if starts[k] is None:
            starts[k] = starts[k + 1]
    return {'wave1_at': starts[1], 'wave2_at': starts[2], 'wave3_at': starts[3]}


def has_audience(poster, policy):
    """False only for a friends-only task from someone with no friends (it would reach nobody)."""
    return policy != Task.WavePolicy.FRIENDS_ONLY or any(_poster_wave_sizes(poster))


def apply_schedule(task):
    """(Re)compute the schedule from task.opened_at; used when a task is posted or re-opened."""
    for field, value in wave_schedule(task.posted_by, task.wave_policy, task.opened_at).items():
        setattr(task, field, value)


def current_wave(task, now=None):
    now = now or timezone.now()
    reached = 0
    for k, at in enumerate([task.opened_at, task.wave1_at, task.wave2_at, task.wave3_at]):
        if at is not None and at <= now:
            reached = k
    return reached


# ---------------------------------------------------------------- circle (read side)

def viewer_circle(profile):
    """
    {poster_user_id: earliest wave this viewer can be in} for everyone who rated the viewer
    (direct friends) or is a friend-of-friend. Cached for CIRCLE_TTL seconds and
    invalidated when friendships change.
    """
    key = _CIRCLE_KEY.format(profile.user_id)
    circle = cache.get(key)
    if circle is not None:
        return circle

    circle = {}
    # Direct friends: the POSTER's rating of the viewer decides the wave.
    for poster_uid, closeness in Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', 'closeness'):
        circle[poster_uid] = wave_for_closeness(closeness)
    # Friends-of-friends: wave 2.
    my_friends = Friendship.objects.filter(from_user=profile).values('to_user')
    for poster_uid in (Friendship.objects.filter(to_user__in=my_friends)
                       .values_list('from_user__user_id', flat=True).distinct()):
        circle.setdefault(poster_uid, 2)
    circle.pop(profile.user_id, None)
    cache.set(key, circle, CIRCLE_TTL)
    return circle


@receiver([post_save, post_delete], sender=Friendship)
def _invalidate_circles(sender, instance, **kwargs):
    """A new/removed friendship changes the circles of both people and of their friends."""
    pids = {instance.from_user_id, instance.to_user_id}
    pids |= set(Friendship.objects.filter(from_user_id__in=pids).values_list('to_user_id', flat=True))
    uids = UserProfile.objects.filter(pk__in=pids).values_list('user_id', flat=True)
    cache.delete_many([_CIRCLE_KEY.format(uid) for uid in uids])


# ---------------------------------------------------------------- feeds

YOUNG_SCAN_LIMIT = 500  # newest not-yet-community-wide tasks inspected per request


def _visible_for(task, circle, now):
    """O(1) visibility check against a cached circle."""
    if task.wave3_at is not None and task.wave3_at <= now:
        return True
    wave = circle.get(task.posted_by_id)
    if wave is None:
        return False
    start = (task.opened_at, task.wave1_at, task.wave2_at)[wave]
    return start is not None and start <= now


def feed_for(user, query=None, limit=20):
    """
    Open tasks the user can currently see, newest first (a list, at most `limit`).

    Two cheap indexed queries instead of one query carrying the whole circle:
      A. tasks whose community wave has started (visible to everyone in the community);
      B. the newest "young" tasks (community wave not started yet), filtered in Python
         against the viewer's cached circle.
    """
    now = timezone.now()
    profile = UserProfile.objects.select_related('community').filter(user=user).first()
    if profile is None:
        profile, _ = UserProfile.objects.get_or_create(user=user)
    community = profile.get_community()
    circle = viewer_circle(profile)

    base = (Task.objects.filter(status=Task.Status.AVAILABLE, community=community)
            .exclude(posted_by=user).select_related('posted_by'))
    if query:
        base = base.filter(Q(title__icontains=query) | Q(description__icontains=query))

    community_wide = list(base.filter(wave3_at__lte=now).order_by('-opened_at')[:limit])
    young_ids = []
    if circle:
        # Scan lightweight tuples; build full objects only for the few that are visible.
        rows = (base.filter(Q(wave3_at__gt=now) | Q(wave3_at__isnull=True), opened_at__lte=now)
                .order_by('-opened_at')
                .values_list('pk', 'posted_by_id', 'opened_at', 'wave1_at', 'wave2_at')[:YOUNG_SCAN_LIMIT])
        for pk, poster_id, *starts in rows:
            wave = circle.get(poster_id)
            if wave is not None and starts[wave] is not None and starts[wave] <= now:
                young_ids.append(pk)
                if len(young_ids) >= limit:
                    break
    young = list(base.filter(pk__in=young_ids)) if young_ids else []
    merged = sorted(community_wide + young, key=lambda t: t.opened_at, reverse=True)
    return merged[:limit]


def public_feed(query=None):
    """For anonymous visitors: default-community tasks that reached the whole community."""
    from ..models import Community
    qs = Task.objects.filter(status=Task.Status.AVAILABLE, community__slug=Community.DEFAULT_SLUG,
                             wave3_at__lte=timezone.now())
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query))
    return qs.order_by('-opened_at')


def can_see(user, task):
    """Can `user` see `task` in their feed right now? (O(1) with a warm circle cache.)"""
    if task.status != Task.Status.AVAILABLE or task.posted_by_id == user.pk:
        return False
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if task.community_id != profile.get_community().pk:
        return False
    return _visible_for(task, viewer_circle(profile), timezone.now())


# ---------------------------------------------------------------- notifications

def schedule_wave_notifications(task):
    """Delayed outbox events that notify each friend circle when its wave opens."""
    now = timezone.now()
    if task.wave_policy == Task.WavePolicy.COMMUNITY_NOW:
        emit('task.wave', {'task_id': task.pk, 'wave': 'all_friends', 'opened_at': task.opened_at.isoformat()})
        return
    starts = {0: task.opened_at, 1: task.wave1_at, 2: task.wave2_at}
    for wave, at in starts.items():
        if at is None:
            continue
        delay = at - now
        emit('task.wave', {'task_id': task.pk, 'wave': wave, 'opened_at': task.opened_at.isoformat()},
             delay=delay if delay.total_seconds() > 0 else None)


def wave_recipients(task, wave):
    """Users who become eligible exactly at `wave` (friends of the poster, by closeness)."""
    rows = Friendship.objects.filter(from_user__user=task.posted_by).select_related('to_user__user')
    return [f.to_user.user for f in rows if wave == 'all_friends' or wave_for_closeness(f.closeness) == wave]
