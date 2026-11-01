from datetime import timedelta
from unittest import mock

from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from ..models import Conversation, Task, TaskEvent
from ..services import jobs, marketplace
from ..services.marketplace import TransitionError
from .base import World


class TaskLifecycleTests(World, TestCase):
    def setUp(self):
        self.make_world()

    def test_posting_reserves_points_in_escrow(self):
        self.assertEqual(self.balance(self.user1), 900)
        self.assertLedgerConsistent()

    def test_state_changing_endpoints_reject_get(self):
        for name in ['take_task', 'submit_task', 'complete_task', 'cancel_task',
                     'request_cancellation', 'accept_cancellation', 'abandon_task']:
            with self.subTest(name=name):
                self.assertEqual(self.c2.get(self.url(name)).status_code, 405)
        self.assertEqual(self.refresh().status, Task.Status.AVAILABLE)

    def test_take_submit_approve_pays_once(self):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('submit_task'), {'note': 'done'})
        self.assertEqual(self.refresh().status, Task.Status.SUBMITTED)
        self.c1.post(self.url('complete_task'))
        self.c1.post(self.url('complete_task'))  # double click
        self.assertEqual(self.refresh().status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()

    def test_poster_can_approve_without_submission(self):
        self.c2.post(self.url('take_task'))
        self.c1.post(self.url('complete_task'))
        self.assertEqual(self.balance(self.user2), 1100)

    def test_cannot_take_own_task(self):
        self.c1.post(self.url('take_task'))
        self.assertEqual(self.refresh().status, Task.Status.AVAILABLE)

    def test_only_poster_can_approve(self):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('complete_task'))
        self.assertEqual(self.refresh().status, Task.Status.IN_PROGRESS)
        self.assertEqual(self.balance(self.user2), 1000)

    def test_accept_cancellation_is_terminal_and_refunds_once(self):
        self.c2.post(self.url('take_task'))
        self.c1.post(self.url('request_cancellation'))
        self.c2.post(self.url('accept_cancellation'))
        self.assertEqual(self.refresh().status, Task.Status.CANCELLED)
        self.assertEqual(self.balance(self.user1), 1000)
        self.c3.post(self.url('take_task'))
        self.c1.post(self.url('complete_task'))
        self.assertEqual(self.balance(self.user3), 1000)
        self.assertLedgerConsistent()

    def test_accept_cancel_requires_request(self):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('accept_cancellation'))
        self.assertEqual(self.refresh().status, Task.Status.IN_PROGRESS)

    def test_cancel_open_task_refunds_once(self):
        self.c1.post(self.url('cancel_task'))
        self.c1.post(self.url('cancel_task'))
        self.assertEqual(self.balance(self.user1), 1000)
        self.assertLedgerConsistent()

    def test_abandon_reopens_and_new_taker_joins_chat(self):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('abandon_task'))
        self.c3.post(self.url('take_task'))
        self.assertEqual(self.refresh().taken_by, self.user3)
        conv = Conversation.objects.get(task=self.task)
        self.assertTrue(conv.participants.filter(pk=self.user3.pk).exists())
        self.assertLedgerConsistent()

    def test_add_task_rejects_insufficient_points(self):
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.c2.post(reverse('add_task'), {'title': 't', 'description': 'd', 'reward': 5000, 'deadline': deadline})
        self.assertEqual(self.balance(self.user2), 1000)
        self.assertFalse(Task.objects.filter(posted_by=self.user2).exists())

    def test_add_task_via_web(self):
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.c2.post(reverse('add_task'), {'title': 't', 'description': 'd', 'reward': 300,
                                           'deadline': deadline, 'wave_policy': 'friends_only'})
        t = Task.objects.get(posted_by=self.user2)
        self.assertEqual(t.wave_policy, 'friends_only')
        self.assertEqual(self.balance(self.user2), 700)
        self.assertLedgerConsistent()

    def test_every_transition_is_audited(self):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('submit_task'))
        actions = list(TaskEvent.objects.filter(task=self.task).values_list('action', flat=True))
        self.assertEqual(actions, ['post', 'accept', 'submit'])

    def test_illegal_transition_raises(self):
        with self.assertRaises(TransitionError):
            marketplace.apply(self.task, 'submit', self.user2)

    def test_allowed_actions(self):
        self.assertEqual(marketplace.allowed_actions(self.task, self.user1), ['cancel'])
        self.assertEqual(marketplace.allowed_actions(self.task, self.user2), ['accept'])


class JobTests(World, TestCase):
    def setUp(self):
        self.make_world()

    def test_auto_approve_after_window(self):
        marketplace.apply(self.task, 'accept', self.user2)
        marketplace.apply(self.task, 'submit', self.user2)
        jobs.run_due()
        self.assertEqual(self.refresh().status, Task.Status.SUBMITTED)
        Task.objects.filter(pk=self.task.pk).update(submitted_at=timezone.now() - timedelta(hours=49))
        report = jobs.run_due()
        self.assertEqual(report['auto_approved'], 1)
        self.assertEqual(self.refresh().status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)

    def test_open_task_expires_after_deadline_and_refunds(self):
        Task.objects.filter(pk=self.task.pk).update(deadline=timezone.now() - timedelta(minutes=1))
        jobs.run_due()
        self.assertEqual(self.refresh().status, Task.Status.EXPIRED)
        self.assertEqual(self.balance(self.user1), 1000)
        self.assertLedgerConsistent()

    def test_system_actions_rejected_for_users(self):
        marketplace.apply(self.task, 'accept', self.user2)
        marketplace.apply(self.task, 'submit', self.user2)
        with self.assertRaises(TransitionError):
            marketplace.apply(self.task, 'auto_approve', self.user1)

    def test_cron_endpoint_requires_secret(self):
        self.assertEqual(self.c1.get(reverse('cron')).status_code, 403)
        with mock.patch.dict('django.conf.settings.LAZYONE', {'CRON_SECRET': 's3cret'}):
            self.assertEqual(self.c1.get(reverse('cron'), HTTP_AUTHORIZATION='Bearer nope').status_code, 403)
            self.assertEqual(self.c1.get(reverse('cron'), HTTP_AUTHORIZATION='Bearer s3cret').status_code, 200)


@skipUnlessDBFeature('has_select_for_update')  # SQLite has no row locks; runs on Postgres in CI
class ConcurrencyTests(World, TransactionTestCase):
    def setUp(self):
        self.make_world()

    def test_two_takers_race(self):
        import threading
        from django.db import connection

        def take(user):
            try:
                marketplace.apply(self.task.pk, 'accept', user)
            except TransitionError:
                pass
            finally:
                connection.close()

        threads = [threading.Thread(target=take, args=(u,)) for u in (self.user2, self.user3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertIn(self.refresh().taken_by, [self.user2, self.user3])
        self.assertEqual(TaskEvent.objects.filter(task=self.task, action='accept').count(), 1)

    def test_double_approve_race_pays_once(self):
        import threading
        from django.db import connection
        marketplace.apply(self.task, 'accept', self.user2)

        def approve():
            try:
                marketplace.apply(self.task.pk, 'approve', self.user1)
            except TransitionError:
                pass
            finally:
                connection.close()

        threads = [threading.Thread(target=approve) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()
