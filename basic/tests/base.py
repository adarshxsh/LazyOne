from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import F
from django.db.models import Sum
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ledger.models import Account
from ledger.services import check_invariants

from ..models import Community, Friendship, UserProfile
from ..services import marketplace


def make_user(name, points=1000, community=None, staff=False):
    user = User.objects.create_user(username=name, email=f"{name}@example.com", password='pw', is_staff=staff)
    UserProfile.objects.create(user=user, rewards=points, community=community or Community.default())
    return user


def befriend(a, b, closeness=50):
    pa, pb = a.userprofile, b.userprofile
    Friendship.objects.create(from_user=pa, to_user=pb, closeness=closeness)
    Friendship.objects.create(from_user=pb, to_user=pa, closeness=closeness)


class World:
    """poster (user1) has posted a 100-point task; taker (user2), outsider (user3)."""

    def make_world(self):
        cache.clear()
        self.user1 = make_user('user1')
        self.user2 = make_user('user2')
        self.user3 = make_user('user3')
        self.task = marketplace.post_task(self.user1, title="Test Task", description="d", reward=100,
                                          deadline=timezone.now() + timedelta(days=1),
                                          wave_policy='community_now')
        self.c1, self.c2, self.c3 = Client(), Client(), Client()
        self.c1.force_login(self.user1)
        self.c2.force_login(self.user2)
        self.c3.force_login(self.user3)

    def url(self, name, obj=None):
        return reverse(name, args=[(obj or self.task).public_id])

    def balance(self, user):
        return UserProfile.objects.get(user=user).rewards

    def refresh(self):
        self.task.refresh_from_db()
        return self.task

    def assertLedgerConsistent(self):
        self.assertEqual(check_invariants(), [])
        minted = -Account.objects.get(key='system:mint').balance
        held = Account.objects.exclude(kind='system').aggregate(s=Sum('balance'))['s'] or 0
        self.assertEqual(held, minted)


def age_task(task, minutes):
    """Pretend the task opened `minutes` ago: shift opened_at and its whole wave schedule."""
    from datetime import timedelta as _td
    from ..models import Task
    t = Task.objects.get(pk=task.pk)
    shift = (timezone.now() - _td(minutes=minutes)) - t.opened_at
    Task.objects.filter(pk=task.pk).update(
        opened_at=F('opened_at') + shift,
        wave1_at=F('wave1_at') + shift, wave2_at=F('wave2_at') + shift, wave3_at=F('wave3_at') + shift)
