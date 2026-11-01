from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from ..models import Community, Notification, Task
from ..services import marketplace
from ..services.feed import can_see, feed_for, public_feed, wave_recipients
from django.core.cache import cache

from .base import age_task, befriend, make_user


class WaveFeedTests(TestCase):
    def setUp(self):
        cache.clear()
        self.poster = make_user('poster')
        self.close = make_user('close')      # closeness 90 -> wave 0
        self.mid = make_user('mid')          # closeness 50 -> wave 1
        self.far = make_user('far')          # closeness 10 -> wave 2
        self.fof = make_user('fof')          # friend of `close` -> wave 2
        self.stranger = make_user('stranger')  # wave 3
        befriend(self.poster, self.close, 90)
        befriend(self.poster, self.mid, 50)
        befriend(self.poster, self.far, 10)
        befriend(self.close, self.fof, 70)

    def post(self, policy='default'):
        return marketplace.post_task(self.poster, title='Buy graph paper', description='', reward=20,
                                     deadline=timezone.now() + timedelta(days=1), wave_policy=policy)

    def age(self, task, minutes):
        age_task(task, minutes)

    def visible(self, user, task):
        task.refresh_from_db()
        in_feed = any(t.pk == task.pk for t in feed_for(user, limit=100))
        self.assertEqual(in_feed, can_see(user, task))  # both paths must agree
        return in_feed

    def test_waves_expand_over_time(self):
        t = self.post()
        expected = {
            0:   {'close'},
            11:  {'close', 'mid'},
            31:  {'close', 'mid', 'far', 'fof'},
            121: {'close', 'mid', 'far', 'fof', 'stranger'},
        }
        users = [self.close, self.mid, self.far, self.fof, self.stranger]
        for minutes, names in expected.items():
            self.age(t, minutes)
            with self.subTest(minutes=minutes):
                self.assertEqual({u.username for u in users if self.visible(u, t)}, names)

    def test_poster_never_sees_own_task_in_feed(self):
        t = self.post('community_now')
        self.assertFalse(self.visible(self.poster, t))

    def test_friends_only_never_reaches_strangers(self):
        t = self.post('friends_only')
        self.age(t, 500)
        self.assertTrue(self.visible(self.fof, t))
        self.assertFalse(self.visible(self.stranger, t))

    def test_community_now_is_immediate(self):
        t = self.post('community_now')
        self.assertTrue(self.visible(self.stranger, t))

    def test_other_communities_never_see_it(self):
        other = Community.objects.create(name='Other', slug='other')
        outsider = make_user('outsider', community=other)
        t = self.post('community_now')
        self.assertFalse(self.visible(outsider, t))

    def test_public_feed_only_last_wave(self):
        t = self.post()
        self.assertFalse(public_feed().filter(pk=t.pk).exists())
        self.age(t, 121)
        self.assertTrue(public_feed().filter(pk=t.pk).exists())

    def test_search(self):
        t = self.post('community_now')
        self.assertIn(t.pk, [x.pk for x in feed_for(self.stranger, 'graph')])
        self.assertNotIn(t.pk, [x.pk for x in feed_for(self.stranger, 'pizza')])

    def test_wave_recipients_and_notifications(self):
        t = self.post()
        self.assertEqual({u.username for u in wave_recipients(t, 0)}, {'close'})
        self.assertEqual({u.username for u in wave_recipients(t, 1)}, {'mid'})
        from outbox.services import process_due
        from outbox.models import OutboxEvent
        OutboxEvent.objects.update(available_at=timezone.now())
        process_due()
        notified = set(Notification.objects.values_list('recipient__username', flat=True))
        self.assertTrue({'close', 'mid', 'far'} <= notified)
        self.assertNotIn('stranger', notified)

    def test_stale_wave_skipped_after_task_taken(self):
        t = self.post()
        marketplace.apply(t, 'accept', self.close)
        from outbox.models import OutboxEvent
        from outbox.services import process_due
        Notification.objects.all().delete()
        OutboxEvent.objects.update(available_at=timezone.now())
        process_due()
        self.assertFalse(Notification.objects.filter(message__contains='needs a hand').exists())


    def set_prestige(self, user, value):
        from ..models import UserProfile
        UserProfile.objects.filter(user=user).update(prestige=value)

    def test_prestige_head_start_on_community_wave_only(self):
        t = self.post()  # community wave at +120 min
        self.set_prestige(self.stranger, 60)    # Trusted -> 10 min early
        elder = make_user('elder')
        self.set_prestige(elder, 600)           # Elder -> 30 min early
        self.age(t, 105)
        self.assertFalse(self.visible(self.stranger, t))
        self.assertTrue(self.visible(elder, t))
        self.age(t, 111)
        self.assertTrue(self.visible(self.stranger, t))

    def test_prestige_never_bypasses_friends_only_or_friend_waves(self):
        t = self.post('friends_only')
        self.set_prestige(self.stranger, 10_000)
        self.age(t, 600)
        self.assertFalse(self.visible(self.stranger, t))
        t2 = self.post()
        self.set_prestige(self.far, 10_000)     # wave 2 friend: head start doesn't touch friend waves
        self.age(t2, 5)
        self.assertFalse(self.visible(self.far, t2))


class AdaptiveScheduleTests(TestCase):
    """Empty waves are skipped so nobody waits for a circle that doesn't exist."""

    def setUp(self):
        cache.clear()
        self.poster = make_user('poster')
        self.stranger = make_user('stranger')

    def post(self, policy='default'):
        return marketplace.post_task(self.poster, title='t', description='', reward=10,
                                     deadline=timezone.now() + timedelta(days=1), wave_policy=policy)

    def offsets(self, t):
        return [None if at is None else round((at - t.opened_at).total_seconds() / 60)
                for at in (t.wave1_at, t.wave2_at, t.wave3_at)]

    def test_full_circle_uses_standard_schedule(self):
        for name, c in [('a', 90), ('b', 50), ('c', 10)]:
            befriend(self.poster, make_user(name), c)
        self.assertEqual(self.offsets(self.post()), [10, 30, 120])

    def test_no_friends_reaches_community_immediately(self):
        t = self.post()
        self.assertEqual(self.offsets(t), [0, 0, 0])
        self.assertIn(t.pk, [x.pk for x in feed_for(self.stranger)])

    def test_no_close_friends_starts_at_wave1(self):
        befriend(self.poster, make_user('mid'), 50)
        befriend(self.poster, make_user('far'), 10)
        self.assertEqual(self.offsets(self.post()), [0, 20, 110])

    def test_only_close_friends_then_community(self):
        befriend(self.poster, make_user('close'), 90)
        # wave 1 and 2 empty -> community follows wave 0 after the wave-3 gap only
        self.assertEqual(self.offsets(self.post()), [90, 90, 90])

    def test_friends_only_without_friends_is_rejected(self):
        from ..services.marketplace import TransitionError
        with self.assertRaises(TransitionError):
            self.post('friends_only')

    def test_friends_only_never_has_community_wave(self):
        befriend(self.poster, make_user('mid'), 50)
        self.assertIsNone(self.post('friends_only').wave3_at)

    def test_reopened_task_recomputes_schedule(self):
        t = self.post()
        self.assertEqual(self.offsets(t), [0, 0, 0])
        taker = make_user('taker')
        befriend(self.poster, make_user('close'), 90)
        marketplace.apply(t, 'accept', taker)
        t = marketplace.apply(t, 'abandon', taker)
        self.assertEqual(self.offsets(t), [90, 90, 90])

    def test_new_friendship_invalidates_cached_circle(self):
        befriend(self.poster, make_user('x'), 10)
        t = self.post()  # x is wave 2 -> starts at 0 since waves 0/1 empty
        viewer = make_user('late')
        self.assertNotIn(t.pk, [x.pk for x in feed_for(viewer)])  # caches empty circle
        befriend(self.poster, viewer, 90)
        self.assertIn(t.pk, [x.pk for x in feed_for(viewer)])


class FeedOrderingTests(TestCase):
    def test_merges_circle_and_community_tasks_newest_first_with_limit(self):
        cache.clear()
        viewer, friend, other = make_user('viewer'), make_user('friend'), make_user('other')
        befriend(friend, viewer, 90)
        posted = []
        for i in range(3):
            t1 = marketplace.post_task(friend, title=f'f{i}', description='', reward=1)  # visible: circle wave 0
            t2 = marketplace.post_task(other, title=f'o{i}', description='', reward=1)   # visible: no friends -> community now
            posted += [t1, t2]
        feed = feed_for(viewer, limit=4)
        self.assertEqual(len(feed), 4)
        opened = [t.opened_at for t in feed]
        self.assertEqual(opened, sorted(opened, reverse=True))
        self.assertEqual({t.title for t in feed_for(viewer, limit=10)}, {t.title for t in posted})
