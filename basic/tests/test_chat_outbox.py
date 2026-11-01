from unittest import mock

from django.test import TestCase
from django.urls import reverse

from outbox.models import OutboxEvent
from outbox.services import emit, handler, process_due

from ..models import Conversation, Message, Notification
from .base import World


class ChatTests(World, TestCase):
    def setUp(self):
        self.make_world()
        self.c2.post(self.url('take_task'))
        self.conv = Conversation.objects.get(task=self.task)

    def test_start_chat_redirects_to_conversation(self):
        resp = self.c1.get(reverse('start_chat', args=[self.user3.id]))
        conv = Conversation.objects.get(task__isnull=True, participants=self.user1)
        self.assertRedirects(resp, reverse('chat_view', args=[conv.public_id]), fetch_redirect_response=False)

    def test_send_stores_in_postgres_and_returns_message(self):
        r = self.c2.post(reverse('send_message', args=[self.conv.public_id]), {'content': 'on my way'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['message']['content'], 'on my way')
        self.assertTrue(Message.objects.filter(conversation=self.conv, content='on my way').exists())
        self.assertTrue(OutboxEvent.objects.filter(topic='message.created').exists())

    def test_non_participant_cannot_send_or_read(self):
        r = self.c3.post(reverse('send_message', args=[self.conv.public_id]), {'content': 'hi'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c3.get(reverse('message_history', args=[self.conv.public_id])).status_code, 403)

    def test_history_and_polling(self):
        for i in range(3):
            self.c2.post(reverse('send_message', args=[self.conv.public_id]), {'content': f'm{i}'})
        msgs = self.c1.get(reverse('message_history', args=[self.conv.public_id])).json()['messages']
        self.assertEqual([m['content'] for m in msgs], ['m0', 'm1', 'm2'])
        after = self.c1.get(reverse('message_history', args=[self.conv.public_id]) + f"?after={msgs[0]['id']}").json()
        self.assertEqual([m['content'] for m in after['messages']], ['m1', 'm2'])

    def test_message_event_publishes_and_notifies_once(self):
        publisher = mock.Mock()
        with mock.patch('basic.handlers.get_publisher', return_value=publisher), \
                self.captureOnCommitCallbacks(execute=True):
            self.c2.post(reverse('send_message', args=[self.conv.public_id]), {'content': 'a'})
        with mock.patch('basic.handlers.get_publisher', return_value=publisher), \
                self.captureOnCommitCallbacks(execute=True):
            self.c2.post(reverse('send_message', args=[self.conv.public_id]), {'content': 'b'})
        self.assertEqual(publisher.publish_message.call_count, 2)
        link = reverse('chat_view', args=[self.conv.public_id])
        self.assertEqual(Notification.objects.filter(recipient=self.user1, link=link).count(), 1)

    def test_firestore_members_follow_permissions(self):
        publisher = mock.Mock()
        with mock.patch('basic.handlers.get_publisher', return_value=publisher):
            process_due()
        uids = publisher.set_conversation_members.call_args[0][1]
        self.assertEqual(set(uids), {f"django:{self.user1.pk}", f"django:{self.user2.pk}"})

    def test_task_notifications_via_outbox(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.c2.post(self.url('submit_task'))
        self.assertTrue(Notification.objects.filter(recipient=self.user1, message__contains='submitted').exists())


class OutboxTests(TestCase):
    def test_failed_handler_retries_with_backoff_then_gives_up(self):
        calls = []

        @handler('test.flaky')
        def flaky(payload):
            calls.append(payload)
            raise RuntimeError('down')

        from outbox import services
        services._load_handlers()
        services._HANDLERS['test.flaky'] = flaky
        ev = OutboxEvent.objects.create(topic='test.flaky', payload={'x': 1})
        with self.assertLogs('outbox.services', 'ERROR'):
            process_due()
        ev.refresh_from_db()
        self.assertEqual(ev.status, 'pending')
        self.assertEqual(ev.attempts, 1)
        self.assertIn('down', ev.last_error)
        with self.assertLogs('outbox.services', 'ERROR'):
            for _ in range(10):
                OutboxEvent.objects.filter(pk=ev.pk).update(available_at=ev.created_at)
                process_due()
        ev.refresh_from_db()
        self.assertEqual(ev.status, 'failed')

    def test_delayed_event_waits(self):
        from datetime import timedelta
        ev = emit('unknown.topic', {}, delay=timedelta(hours=1))
        process_due()
        ev.refresh_from_db()
        self.assertEqual(ev.status, 'pending')


class AuthPagesTests(TestCase):
    def test_logout_redirects_to_login(self):
        from .base import make_user
        u = make_user('leaving')
        self.client.force_login(u)
        r = self.client.get(reverse('logout'))
        self.assertRedirects(r, reverse('account_login'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_alias_redirects_to_allauth(self):
        r = self.client.get('/login/?next=/my_tasks/')
        self.assertRedirects(r, reverse('account_login') + '?next=/my_tasks/', fetch_redirect_response=False)
