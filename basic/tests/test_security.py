import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.conf import settings
from django.urls import reverse
from django.core.cache import cache

from ledger.services import balance_of, check_invariants
from ..models import Community, UserProfile
from .base import befriend, make_user

HOLD_ON = {**settings.LAZYONE, 'SIGNUP_BONUS_REQUIRES_VERIFICATION': True, 'UNVERIFIED_STARTER_POINTS': 100}


@override_settings(LAZYONE=HOLD_ON)
class SignupBonusTests(TestCase):
    def new_user(self, name, community=None):
        u = User.objects.create_user(username=name, email=f'{name}@example.com', password='pw')
        UserProfile.objects.create(user=u, community=community or Community.default())
        return u

    def test_unverified_gets_starter_only_then_bonus_on_phone_verification(self):
        u = self.new_user('alt')
        self.assertEqual(balance_of(u), 100)
        self.assertEqual(UserProfile.objects.get(user=u).rewards, 100)
        p = UserProfile.objects.get(user=u)
        p.is_phone_verified = True
        p.save()
        self.assertEqual(balance_of(u), 1500)
        p.save()  # idempotent: no second release
        self.assertEqual(balance_of(u), 1500)
        self.assertEqual(check_invariants(), [])

    def test_campus_community_gets_full_bonus(self):
        campus = Community.objects.create(name='IITJ', slug='iitj', email_domain='iitj.ac.in')
        u = self.new_user('stu', community=campus)
        self.assertEqual(balance_of(u), 1500)


class PrivacyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = make_user('me')
        self.friend = make_user('friend')
        self.oneway = make_user('oneway')
        self.stranger = make_user('stranger')
        other = Community.objects.create(name='Other', slug='other')
        self.outsider = make_user('outsider', community=other)
        for u in (self.friend, self.oneway, self.stranger, self.outsider):
            UserProfile.objects.filter(user=u).update(phone_number=f'+9100000{u.pk:05d}', instagram_username=f'ig_{u.username}')
        befriend(self.me, self.friend)
        from ..models import Friendship
        Friendship.objects.create(from_user=self.me.userprofile, to_user=self.oneway.userprofile, closeness=50)
        self.client.force_login(self.me)

    def nodes(self):
        r = self.client.get(reverse('home'))
        return {n['username']: n for n in json.loads(r.context['user_nodes_json'])}

    def test_home_graph_shares_contacts_with_mutual_friends_only(self):
        nodes = self.nodes()
        self.assertTrue(nodes['friend']['phone'])
        self.assertIsNone(nodes['oneway']['phone'])
        self.assertIsNone(nodes['stranger']['phone'])
        self.assertIsNone(nodes['stranger']['instagram'])
        self.assertNotIn('outsider', nodes)  # other communities aren't listed

    def test_find_people_page_has_no_contact_details(self):
        r = self.client.get(reverse('user_list'))
        html = r.content.decode()
        self.assertIn('stranger', html)
        self.assertNotIn('outsider', html)
        self.assertNotIn('+91', html)
        self.assertNotIn('ig_', html)
        self.assertNotIn('firestore', html.lower())

    def test_phone_number_verifies_only_one_account(self):
        from unittest import mock
        UserProfile.objects.filter(user=self.friend).update(phone_number='+911234567890', is_phone_verified=True)
        ok = {'status': 'success', 'message': '', 'phone_number': '+911234567890'}
        with mock.patch('basic.services.phone_verification.PhoneVerificationService.verify_and_extract_phone', return_value=ok):
            r = self.client.post(reverse('verify_phone_token'), json.dumps({'token': 'x'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(UserProfile.objects.get(user=self.me).is_phone_verified)


class WebRateLimitTests(TestCase):
    def test_send_friend_request_is_limited(self):
        cache.clear()
        me = make_user('spammer')
        targets = [make_user(f't{i}') for i in range(32)]
        self.client.force_login(me)
        codes = [self.client.post(reverse('send_friend_request', args=[t.pk]), {'closeness': 50}).status_code for t in targets]
        from ..models import FriendRequest
        self.assertEqual(FriendRequest.objects.filter(from_user=me).count(), 30)
