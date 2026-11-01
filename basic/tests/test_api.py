from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import IdempotencyRecord

from ..models import Dispute, Task, UserProfile
from ..services.identity import user_from_firebase_claims
from .base import World, make_user


class ApiTests(World, TestCase):
    def setUp(self):
        cache.clear()
        self.make_world()
        self.a1, self.a2, self.a3 = APIClient(), APIClient(), APIClient()
        self.a1.force_authenticate(self.user1)
        self.a2.force_authenticate(self.user2)
        self.a3.force_authenticate(self.user3)

    def test_requires_auth(self):
        self.assertIn(APIClient().get('/api/v1/me/').status_code, (401, 403))

    def test_me(self):
        r = self.a1.get('/api/v1/me/').json()
        self.assertEqual(r['balance'], 900)
        self.assertEqual(r['community']['slug'], 'public')

    def test_full_lifecycle(self):
        feed = self.a2.get('/api/v1/tasks/feed/').json()
        self.assertEqual([t['public_id'] for t in feed], [self.task.public_id])
        self.assertEqual(feed[0]['allowed_actions'], ['accept'])
        r = self.a2.post(f'/api/v1/tasks/{self.task.public_id}/accept/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNotNone(r.json()['conversation_id'])
        self.a2.post(f'/api/v1/tasks/{self.task.public_id}/submit/', {'note': 'done'})
        r = self.a1.post(f'/api/v1/tasks/{self.task.public_id}/approve/')
        self.assertEqual(r.json()['status'], 'completed')
        self.assertEqual(self.a2.get('/api/v1/me/').json()['balance'], 1100)
        events = self.a1.get(f'/api/v1/tasks/{self.task.public_id}/events/').json()
        self.assertEqual([e['action'] for e in events], ['post', 'accept', 'submit', 'approve'])
        self.assertLedgerConsistent()

    def test_illegal_action_is_409(self):
        r = self.a2.post(f'/api/v1/tasks/{self.task.public_id}/approve/')
        self.assertEqual(r.status_code, 409)

    def test_unknown_action_404(self):
        self.assertEqual(self.a2.post(f'/api/v1/tasks/{self.task.public_id}/expire/').status_code, 404)

    def test_stale_version_is_409(self):
        r = self.a2.post(f'/api/v1/tasks/{self.task.public_id}/accept/', {'version': 99})
        self.assertEqual(r.status_code, 409)

    def test_create_task_and_idempotent_retry(self):
        body = {'title': 'Print notes', 'reward': 40, 'wave_policy': 'community_now',
                'deadline': (timezone.now() + timedelta(days=1)).isoformat()}
        r1 = self.a2.post('/api/v1/tasks/', body, format='json', HTTP_IDEMPOTENCY_KEY='k1')
        r2 = self.a2.post('/api/v1/tasks/', body, format='json', HTTP_IDEMPOTENCY_KEY='k1')
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.headers.get('Idempotent-Replay'), 'true')
        self.assertEqual(Task.objects.filter(posted_by=self.user2).count(), 1)
        self.assertEqual(self.balance(self.user2), 960)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)

    def test_idempotency_key_reuse_on_other_path_rejected(self):
        self.a2.post(f'/api/v1/tasks/{self.task.public_id}/accept/', HTTP_IDEMPOTENCY_KEY='k2')
        r = self.a2.post('/api/v1/tasks/', {'title': 'x', 'reward': 1}, format='json', HTTP_IDEMPOTENCY_KEY='k2')
        self.assertEqual(r.status_code, 422)

    def test_create_task_insufficient_points(self):
        r = self.a2.post('/api/v1/tasks/', {'title': 'x', 'reward': 99999}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_task_detail_hidden_until_visible(self):
        hidden = make_user('poster2')
        from .base import befriend
        befriend(hidden, self.user1, 90)
        from ..services import marketplace
        t = marketplace.post_task(hidden, title='secret', description='', reward=10, wave_policy='friends_only')
        self.assertEqual(self.a3.get(f'/api/v1/tasks/{t.public_id}/').status_code, 404)

    def test_messages_api(self):
        self.a2.post(f'/api/v1/tasks/{self.task.public_id}/accept/')
        conv = self.a2.get(f'/api/v1/tasks/{self.task.public_id}/').json()['conversation_id']
        r = self.a2.post(f'/api/v1/conversations/{conv}/messages/', {'content': 'hello'})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.a1.get(f'/api/v1/conversations/{conv}/messages/').json()[0]['content'], 'hello')
        self.assertEqual(self.a3.get(f'/api/v1/conversations/{conv}/messages/').status_code, 404)
        self.assertEqual(self.a3.post(f'/api/v1/conversations/{conv}/messages/', {'content': 'x'}).status_code, 403)

    def test_dispute_api_v2_flow(self):
        from .base import make_juror
        for i in range(5):
            make_juror(f'j{i}')
        self.a2.post(f'/api/v1/tasks/{self.task.public_id}/accept/')
        r = self.a2.post(f'/api/v1/tasks/{self.task.public_id}/disputes/', {'reason': 'late', 'taker_pct': 80})
        self.assertEqual(r.status_code, 201, r.content)
        did = r.json()['public_id']
        self.assertEqual((r.json()['stage'], r.json()['offers'][0]['taker_pct']), ('settlement', 80))
        r = self.a1.post(f'/api/v1/disputes/{did}/reject/')
        self.assertEqual(r.json()['stage'], 'jury')
        self.assertEqual(r.json()['tally'], {'jurors': 3, 'votes_cast': 0})
        for seat in Dispute.objects.get(public_id=did).jurors.select_related('juror'):
            c = APIClient(); c.force_authenticate(seat.juror)
            resp = c.post(f'/api/v1/disputes/{did}/vote/', {'taker_pct': 100, 'reason': 'Delivered per chat'})
            self.assertEqual(resp.status_code, 200, resp.content)
        d = self.a1.get(f'/api/v1/disputes/{did}/').json()
        self.assertEqual((d['stage'], d['outcome_pct'], d['can_appeal']), ('appeal_window', 100, True))
        self.assertEqual(len(d['tally']['votes']), 3)
        self.assertLedgerConsistent()

    def test_me_shows_prestige(self):
        r = self.a1.get('/api/v1/me/').json()
        self.assertEqual((r['prestige'], r['tier']), (0, 'Newcomer'))

    def test_ledger_history(self):
        r = self.a1.get('/api/v1/ledger/').json()
        self.assertEqual(r['balance'], 900)
        self.assertEqual([e['amount'] for e in r['entries']], [-100, 1000])

    def test_realtime_token_unavailable_without_firebase(self):
        with mock.patch('api.views.create_custom_token', return_value=None):
            self.assertEqual(self.a1.get('/api/v1/realtime/token/').status_code, 503)


class IdentityTests(TestCase):
    def test_new_firebase_user_created_with_wallet(self):
        u = user_from_firebase_claims({'uid': 'abc', 'email': 'new@x.com', 'email_verified': True})
        self.assertEqual(u.userprofile.firebase_uid, 'abc')
        self.assertEqual(u.userprofile.rewards, 1500)
        self.assertEqual(user_from_firebase_claims({'uid': 'abc'}), u)

    def test_links_existing_account_by_verified_email_once(self):
        existing = make_user('old')
        u = user_from_firebase_claims({'uid': 'u1', 'email': existing.email, 'email_verified': True})
        self.assertEqual(u, existing)
        self.assertIsNone(user_from_firebase_claims({'uid': 'u2', 'email': existing.email, 'email_verified': True}))

    def test_unverified_email_does_not_link(self):
        existing = make_user('old2')
        u = user_from_firebase_claims({'uid': 'u3', 'email': existing.email, 'email_verified': False})
        self.assertNotEqual(u, existing)
        self.assertEqual(UserProfile.objects.get(user=u).firebase_uid, 'u3')

    def test_email_domain_assigns_community(self):
        from ..models import Community
        campus = Community.objects.create(name='IITJ', slug='iitj', email_domain='iitj.ac.in')
        u = user_from_firebase_claims({'uid': 'c1', 'email': 'stu@iitj.ac.in', 'email_verified': True})
        self.assertEqual(u.userprofile.community, campus)
