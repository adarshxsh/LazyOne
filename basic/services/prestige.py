"""
Prestige: reputation that matters more than points (docs/Ideation/DISPUTE_RESOLUTION.md §Prestige).

- Append-only PrestigeEvent log; UserProfile.prestige caches the sum.
- Every award has an idempotency key, so retries never count twice.
- Positive awards between the same two people are capped per 30 days, so alt accounts
  can't farm prestige by trading tasks with each other.
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from ..models import PrestigeEvent, UserProfile

# Points of prestige per event (tunable in settings.LAZYONE['PRESTIGE'])
DEFAULTS = {
    'task_completed_taker': 10,
    'task_completed_poster': 3,
    'task_abandoned': -5,
    'auto_approved_poster': -2,     # poster ignored a submission for 48 h
    'dispute_settled': 2,           # resolved between the parties
    'dispute_lost': -20,
    'dispute_default': -10,         # ignored a dispute until the deadline
    'juror_coherent': 5,
    'juror_no_show': -5,
}
PAIR_CAP = 3            # max positive awards per counterparty pair ...
PAIR_WINDOW_DAYS = 30   # ... within this window

TIERS = [(500, 'Elder'), (200, 'Respected'), (50, 'Trusted'), (0, 'Newcomer')]


def points(reason):
    return settings.LAZYONE.get('PRESTIGE', {}).get(reason, DEFAULTS[reason])


def tier(prestige):
    for floor, name in TIERS:
        if prestige >= floor:
            return name
    return 'Newcomer'


def award(user, reason, *, key, task=None, dispute=None, counterparty=None):
    """Apply a prestige event once. Returns the delta actually applied (0 if capped or duplicate)."""
    if user is None:
        return 0
    delta = points(reason)
    if delta > 0 and counterparty is not None:
        since = timezone.now() - timedelta(days=PAIR_WINDOW_DAYS)
        recent = PrestigeEvent.objects.filter(user=user, counterparty=counterparty, delta__gt=0,
                                              created_at__gte=since).count()
        if recent >= PAIR_CAP:
            delta = 0
    try:
        with transaction.atomic():
            PrestigeEvent.objects.create(user=user, delta=delta, reason=reason, key=key,
                                         task=task, dispute=dispute, counterparty=counterparty)
            if delta:
                UserProfile.objects.filter(user=user).update(prestige=F('prestige') + delta)
    except IntegrityError:
        return 0  # already applied
    return delta


def of(user):
    return UserProfile.objects.filter(user=user).values_list('prestige', flat=True).first() or 0
