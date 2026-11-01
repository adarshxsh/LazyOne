from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from ..models import Community, Notification, Task
from ..services import marketplace
from ..services.feed import feed_for, public_feed, wave_recipients
from .base import befriend, make_user


class WaveFeedTests(TestCase):
    def setUp(self):
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
        Task.objects.filter(pk=task.pk).update(opened_at=timezone.now() - timedelta(minutes=minutes))

    def visible(self, user, task):
        return feed_for(user).filter(pk=task.pk).exists()

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
        self.assertTrue(feed_for(self.stranger, 'graph').filter(pk=t.pk).exists())
        self.assertFalse(feed_for(self.stranger, 'pizza').filter(pk=t.pk).exists())

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
