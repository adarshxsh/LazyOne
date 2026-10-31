import threading
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from .models import Conversation, Dispute, Task, UserProfile
from .services.permissions import DisputeService


def total_points():
    """Points in wallets + points held by open/in-flight tasks. Must never change."""
    wallets = UserProfile.objects.aggregate(s=Sum('rewards'))['s'] or 0
    held = Task.objects.filter(
        status__in=[Task.Status.AVAILABLE, Task.Status.IN_PROGRESS, Task.Status.DISPUTED]
    ).aggregate(s=Sum('reward'))['s'] or 0
    return wallets + held


class _Base:
    """Poster (user1) has posted a 100-point task; their wallet already reflects the reservation."""

    def make_world(self):
        self.user1 = User.objects.create_user(username='user1', password='password')
        self.user2 = User.objects.create_user(username='user2', password='password')
        self.user3 = User.objects.create_user(username='user3', password='password')
        UserProfile.objects.create(user=self.user1, rewards=900)
        UserProfile.objects.create(user=self.user2, rewards=1000)
        UserProfile.objects.create(user=self.user3, rewards=1000)
        self.task = Task.objects.create(
            title="Test Task", description="Test Description", reward=100,
            posted_by=self.user1, deadline=timezone.now() + timedelta(days=1),
            status=Task.Status.AVAILABLE,
        )
        self.c1, self.c2, self.c3 = Client(), Client(), Client()
        self.c1.force_login(self.user1)
        self.c2.force_login(self.user2)
        self.c3.force_login(self.user3)
        self.initial_total = total_points()

    def url(self, name, obj=None):
        return reverse(name, args=[(obj or self.task).public_id])

    def balance(self, user):
        return UserProfile.objects.get(user=user).rewards

    def assertPointsConserved(self):
        self.assertEqual(total_points(), self.initial_total)


@patch('basic.models.sync_dispute_to_firestore')
class TaskLifecycleTests(_Base, TestCase):
    def setUp(self):
        self.make_world()

    def test_state_changing_endpoints_reject_get(self, _sync):
        for name in ['take_task', 'complete_task', 'cancel_task',
                     'request_cancellation', 'accept_cancellation', 'abandon_task']:
            with self.subTest(name=name):
                self.assertEqual(self.c2.get(self.url(name)).status_code, 405)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.AVAILABLE)

    def test_take_and_complete_pays_once(self, _sync):
        self.c2.post(self.url('take_task'))
        self.c1.post(self.url('complete_task'))
        self.c1.post(self.url('complete_task'))  # double click
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertPointsConserved()

    def test_cannot_take_own_task(self, _sync):
        self.c1.post(self.url('take_task'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.AVAILABLE)

    def test_only_poster_can_complete(self, _sync):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('complete_task'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)
        self.assertEqual(self.balance(self.user2), 1000)

    def test_accept_cancellation_is_terminal_and_refunds_once(self, _sync):
        """Regression: task used to be refunded AND reopened, minting points for the next taker."""
        self.c2.post(self.url('take_task'))
        self.c1.post(self.url('request_cancellation'))
        self.c2.post(self.url('accept_cancellation'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.CANCELLED)
        self.assertEqual(self.balance(self.user1), 1000)
        # Nobody can take and get paid for it afterwards.
        self.c3.post(self.url('take_task'))
        self.c1.post(self.url('complete_task'))
        self.assertEqual(self.balance(self.user3), 1000)
        self.assertPointsConserved()

    def test_cancel_open_task_refunds(self, _sync):
        self.c1.post(self.url('cancel_task'))
        self.c1.post(self.url('cancel_task'))
        self.assertEqual(self.balance(self.user1), 1000)
        self.assertPointsConserved()

    def test_abandon_reopens_and_new_taker_joins_chat(self, _sync):
        self.c2.post(self.url('take_task'))
        self.c2.post(self.url('abandon_task'))
        self.c3.post(self.url('take_task'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.taken_by, self.user3)
        conv = Conversation.objects.get(task=self.task)
        self.assertTrue(conv.participants.filter(pk=self.user3.pk).exists())
        self.assertPointsConserved()

    def test_add_task_rejects_insufficient_points(self, _sync):
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.c2.post(reverse('add_task'), {'title': 't', 'description': 'd', 'reward': 5000, 'deadline': deadline})
        self.assertEqual(self.balance(self.user2), 1000)
        self.assertFalse(Task.objects.filter(posted_by=self.user2).exists())

    def test_add_task_reserves_points(self, _sync):
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.c2.post(reverse('add_task'), {'title': 't', 'description': 'd', 'reward': 300, 'deadline': deadline})
        self.assertEqual(self.balance(self.user2), 700)
        self.assertPointsConserved()


@patch('basic.models.sync_dispute_to_firestore')
class DisputeTests(_Base, TestCase):
    def setUp(self):
        self.make_world()
        self.c2.post(self.url('take_task'))

    def raise_dispute(self, client):
        client.post(self.url('raise_dispute'), {'reason': 'Payment delayed'})
        return Dispute.objects.get(task=self.task)

    def test_raise_dispute(self, _sync):
        dispute = self.raise_dispute(self.c2)
        self.assertEqual(dispute.raised_by, self.user2)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DISPUTED)

    def test_outsider_cannot_raise_dispute(self, _sync):
        self.c3.post(self.url('raise_dispute'), {'reason': 'x'})
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_withdraw_open_dispute(self, _sync):
        dispute = self.raise_dispute(self.c2)
        self.c2.post(self.url('withdraw_dispute', dispute))
        self.task.refresh_from_db(); dispute.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)
        self.assertEqual(dispute.status, Dispute.Status.WITHDRAWN)

    def test_cannot_withdraw_resolved_dispute(self, _sync):
        """Regression: withdrawing a resolved dispute reopened a paid task -> double payout."""
        dispute = self.raise_dispute(self.c2)
        DisputeService.resolve_dispute(dispute, winner=self.user2)
        self.assertEqual(self.balance(self.user2), 1100)

        self.c2.post(self.url('withdraw_dispute', dispute))
        self.c1.post(self.url('complete_task'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertPointsConserved()

    def test_resolve_for_poster_refunds(self, _sync):
        dispute = self.raise_dispute(self.c2)
        DisputeService.resolve_dispute(dispute, winner=self.user1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.CANCELLED)
        self.assertEqual(self.balance(self.user1), 1000)
        self.assertPointsConserved()

    def test_resolve_twice_fails(self, _sync):
        dispute = self.raise_dispute(self.c2)
        DisputeService.resolve_dispute(dispute, winner=self.user2)
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(dispute, winner=self.user2)
        self.assertEqual(self.balance(self.user2), 1100)

    def test_resolve_rejects_non_party_winner(self, _sync):
        dispute = self.raise_dispute(self.c2)
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(dispute, winner=self.user3)
        self.assertPointsConserved()

    def test_poster_completing_closes_dispute(self, _sync):
        dispute = self.raise_dispute(self.c2)
        self.c1.post(self.url('complete_task'))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.Status.RESOLVED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertPointsConserved()


class ChatTests(_Base, TestCase):
    def setUp(self):
        self.make_world()

    def test_start_chat_redirects_to_conversation(self):
        """Regression: start_chat raised NoReverseMatch on every call."""
        resp = self.c1.get(reverse('start_chat', args=[self.user2.id]))
        conv = Conversation.objects.get(task__isnull=True, participants=self.user1)
        self.assertRedirects(resp, reverse('chat_view', args=[conv.public_id]), fetch_redirect_response=False)

    def test_non_participant_cannot_send(self):
        self.c2.post(self.url('take_task'))
        conv = Conversation.objects.get(task=self.task)
        resp = self.c3.post(reverse('send_message', args=[conv.public_id]), {'content': 'hi'})
        self.assertEqual(resp.status_code, 403)


@skipUnlessDBFeature('has_select_for_update')  # SQLite has no row locks; runs on Postgres in CI
class ConcurrencyTests(_Base, TransactionTestCase):
    def setUp(self):
        self.make_world()

    def test_task_assignment_race_condition(self):
        def take(client):
            client.post(self.url('take_task'))

        threads = [threading.Thread(target=take, args=(c,)) for c in (self.c2, self.c3)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)
        self.assertIn(self.task.taken_by, [self.user2, self.user3])
