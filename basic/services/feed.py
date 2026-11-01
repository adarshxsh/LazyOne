"""
Wave distribution (ARCHITECTURE_V2 §4.3) - LazyOne's core idea.

A task reaches the poster's closest friends first and spreads outward over time:

    wave 0  t+0        friends the poster rates closeness >= 80
    wave 1  t+10 min   friends with closeness >= 40
    wave 2  t+30 min   all friends + friends-of-friends
    wave 3  t+2 h      everyone in the community

Visibility is computed at query time from (now - opened_at), so no timers or fan-out
writes are needed to make a task visible. Only the optional per-wave notifications are
scheduled (as delayed outbox events).
"""
from django.conf import settings
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from outbox.services import emit

from ..models import Friendship, Task, UserProfile

LAST_WAVE = 3


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


def current_wave_expr(now):
    schedule = _cfg()['WAVE_DELAYS']  # [timedelta for wave 0, 1, 2, 3]
    whens = [When(opened_at__lte=now - schedule[w], then=Value(w)) for w in range(LAST_WAVE, 0, -1)]
    return Case(*whens, default=Value(0), output_field=IntegerField())


def current_wave(task, now=None):
    now = now or timezone.now()
    age = now - task.opened_at
    schedule = _cfg()['WAVE_DELAYS']
    return max(w for w in range(LAST_WAVE + 1) if age >= schedule[w])


def feed_for(user, query=None):
    """Open tasks the user can currently see, newest first."""
    now = timezone.now()
    profile, _ = UserProfile.objects.get_or_create(user=user)
    community = profile.get_community()
    c = _cfg()

    closeness = Friendship.objects.filter(from_user__user=OuterRef('posted_by'), to_user=profile).values('closeness')[:1]
    my_friends = Friendship.objects.filter(from_user=profile).values('to_user')
    friend_of_friend = Exists(Friendship.objects.filter(from_user__user=OuterRef('posted_by'), to_user__in=my_friends))

    qs = (Task.objects.filter(status=Task.Status.AVAILABLE, community=community)
          .exclude(posted_by=user)
          .select_related('posted_by')
          .annotate(closeness=Subquery(closeness), fof=friend_of_friend)
          .annotate(min_wave=Case(
              When(wave_policy=Task.WavePolicy.COMMUNITY_NOW, then=Value(0)),
              When(closeness__gte=c['CLOSENESS_WAVE0'], then=Value(0)),
              When(closeness__gte=c['CLOSENESS_WAVE1'], then=Value(1)),
              When(closeness__isnull=False, then=Value(2)),
              When(fof=True, then=Value(2)),
              default=Value(LAST_WAVE), output_field=IntegerField()))
          .annotate(current_wave=current_wave_expr(now))
          .filter(min_wave__lte=F('current_wave'))
          .exclude(wave_policy=Task.WavePolicy.FRIENDS_ONLY, min_wave=LAST_WAVE))
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query))
    return qs.order_by('-opened_at')


def public_feed(query=None):
    """For anonymous visitors: tasks in the default community that reached the last wave."""
    from ..models import Community
    now = timezone.now()
    qs = (Task.objects.filter(status=Task.Status.AVAILABLE, community__slug=Community.DEFAULT_SLUG)
          .exclude(wave_policy=Task.WavePolicy.FRIENDS_ONLY)
          .annotate(current_wave=current_wave_expr(now))
          .filter(Q(current_wave=LAST_WAVE) | Q(wave_policy=Task.WavePolicy.COMMUNITY_NOW)))
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query))
    return qs.order_by('-opened_at')


def can_see(user, task):
    return feed_for(user).filter(pk=task.pk).exists()


def schedule_wave_notifications(task):
    """Delayed outbox events that notify each friend circle when its wave opens."""
    delays = _cfg()['WAVE_DELAYS']
    if task.wave_policy == Task.WavePolicy.COMMUNITY_NOW:
        emit('task.wave', {'task_id': task.pk, 'wave': 'all_friends', 'opened_at': task.opened_at.isoformat()})
        return
    for wave in (0, 1, 2):
        emit('task.wave', {'task_id': task.pk, 'wave': wave, 'opened_at': task.opened_at.isoformat()},
             delay=delays[wave] if delays[wave].total_seconds() else None)


def wave_recipients(task, wave):
    """Users who become eligible exactly at `wave` (friends of the poster, by closeness)."""
    rows = Friendship.objects.filter(from_user__user=task.posted_by).select_related('to_user__user')
    users = []
    for f in rows:
        w = wave_for_closeness(f.closeness)
        if wave == 'all_friends' or w == wave:
            users.append(f.to_user.user)
    return users
