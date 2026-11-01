from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import (Community, Conversation, Dispute, JurorAssignment, PrestigeEvent, SettlementOffer, Task,
                      UserProfile)
from ..services import disputes, marketplace, prestige
from ..services.marketplace import TransitionError
from ..services.permissions import ChatService, DisputeService
from .base import World, befriend, make_juror, make_user


def p(user):
    return UserProfile.objects.get(user=user).prestige


class DisputeBase(World):
    def setUp(self):
        self.make_world()
        self.pool = [make_juror(f'j{i}') for i in range(12)]
        self.c2.post(self.url('take_task'))

    def raise_(self, client=None, reason='Payment delayed', pct=None):
        data = {'reason': reason}
        if pct is not None:
            data['taker_pct'] = pct
        (client or self.c2).post(self.url('raise_dispute'), data)
        return Dispute.objects.filter(task=self.task).latest('created_at')

    def to_jury(self, d):
        disputes.reject_offers(d, self.user1)
        d.refresh_from_db()
        return d

    def vote(self, d, votes):
        seats = list(d.jurors.filter(round=d.round).select_related('juror').order_by('id'))
        for seat, v in zip(seats, votes):
            disputes.cast_vote(d, seat.juror, v, "Looked at the chat and evidence")
        d.refresh_from_db()
        return seats

    def expire(self, d, field):
        Dispute.objects.filter(pk=d.pk).update(**{field: timezone.now() - timedelta(seconds=1)})
        disputes.close_due_disputes()
        d.refresh_from_db()
        return d


class SettlementTests(DisputeBase, TestCase):
    def test_raise_opens_settlement_with_bond_and_opening_offer(self):
        d = self.raise_()
        self.assertEqual((d.stage, d.status, d.bond_amount), ('settlement', 'open', 50))
        self.assertEqual(self.balance(self.user2), 950)
        self.assertEqual(d.offers.get().taker_pct, 100)  # taker raised: defaults fully in their favour
        self.assertEqual(d.jurors.count(), 0)            # no jury until settlement fails
        self.assertLedgerConsistent()

    def test_counter_offer_accepted_splits_reward_refunds_bond_and_rewards_both(self):
        d = self.raise_()
        disputes.make_offer(d, self.user1, 60, "You did most of it")
        disputes.accept_offer(d, self.user2)
        d.refresh_from_db()
        self.assertEqual((d.outcome, d.outcome_pct, d.stage), ('settled', 60, 'closed'))
        self.assertEqual(self.refresh().status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1060)  # 60 % of 100, bond back
        self.assertEqual(self.balance(self.user1), 940)   # 900 + 40 refund
        self.assertEqual((p(self.user1), p(self.user2)), (2, 2))
        self.assertLedgerConsistent()

    def test_cannot_accept_own_offer(self):
        d = self.raise_()
        with self.assertRaises(TransitionError):
            disputes.accept_offer(d, self.user2)

    def test_outsider_cannot_negotiate(self):
        d = self.raise_()
        with self.assertRaises(TransitionError):
            disputes.make_offer(d, self.user3, 50)

    def test_silent_respondent_loses_by_default(self):
        d = self.expire(self.raise_(), 'settlement_deadline')
        self.assertEqual((d.outcome, d.outcome_pct), ('default', 100))
        self.assertEqual(self.balance(self.user2), 1100)  # reward + bond back
        self.assertEqual(p(self.user1), -10)
        self.assertLedgerConsistent()

    def test_engaged_respondent_goes_to_jury_at_deadline(self):
        d = self.raise_()
        disputes.add_evidence(d, self.user1, "It was never delivered")
        d = self.expire(d, 'settlement_deadline')
        self.assertEqual(d.stage, 'jury')
        self.assertEqual(d.jurors.count(), 3)

    def test_jury_size_by_reward(self):
        self.assertEqual([disputes.jury_size_for(r) for r in (10, 49, 50, 299, 300, 5000)], [1, 1, 3, 3, 5, 5])


class JuryTests(DisputeBase, TestCase):
    def test_vote_needs_reason_and_valid_choice(self):
        d = self.to_jury(self.raise_())
        juror = d.jurors.first().juror
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, juror, 100, "ok")
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, juror, 70, "a valid looking reason")
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, make_user('notjuror'), 100, "a valid looking reason")

    def test_median_verdict_waits_for_appeal_window_then_pays(self):
        d = self.to_jury(self.raise_())
        seats = self.vote(d, [100, 100, 0])
        self.assertEqual((d.stage, d.outcome_pct), ('appeal_window', 100))
        self.assertEqual(self.balance(self.user2), 950)  # nothing moves during the appeal window
        d = self.expire(d, 'appeal_deadline')
        self.assertEqual((d.stage, d.outcome), ('closed', 'jury'))
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertEqual(p(self.user1), -20)
        self.assertEqual(sorted(p(s.juror) for s in seats), [50, 55, 55])
        self.assertLedgerConsistent()

    def test_split_verdict(self):
        d = self.expire(self.vote_and_get(self.to_jury(self.raise_()), [50, 50, 100]), 'appeal_deadline')
        self.assertEqual(d.outcome_pct, 50)
        self.assertEqual((self.balance(self.user2), self.balance(self.user1)), (1050, 950))
        self.assertEqual((p(self.user1), p(self.user2)), (0, 0))  # nobody "lost"
        self.assertLedgerConsistent()

    def vote_and_get(self, d, votes):
        self.vote(d, votes)
        return d

    def test_verdict_against_raiser_shares_bond_with_coherent_jurors(self):
        d = self.to_jury(self.raise_())
        seats = self.vote(d, [0, 0, 100])
        d = self.expire(d, 'appeal_deadline')
        self.assertEqual(self.refresh().status, Task.Status.CANCELLED)
        self.assertEqual((self.balance(self.user1), self.balance(self.user2)), (1000, 950))
        self.assertEqual(sorted(JurorAssignment.objects.filter(dispute=d).values_list('reward', flat=True)), [0, 25, 25])
        self.assertEqual(p(self.user2), -20)
        self.assertLedgerConsistent()

    def test_no_quorum_escalates_and_no_shows_lose_prestige(self):
        d = self.to_jury(self.raise_())
        seats = self.vote(d, [100])
        d = self.expire(d, 'voting_deadline')
        self.assertEqual(d.status, 'escalated')
        self.assertEqual(sorted(p(s.juror) for s in seats), [45, 45, 50])
        disputes.moderator_resolve(d, moderator=make_user('mod', staff=True), taker_pct=75)
        d.refresh_from_db()
        self.assertEqual((d.outcome, d.outcome_pct), ('moderator', 75))
        self.assertLedgerConsistent()

    def test_two_different_votes_without_majority_escalates(self):
        d = self.to_jury(self.raise_())
        self.vote(d, [0, 100])
        d = self.expire(d, 'voting_deadline')
        self.assertEqual(d.status, 'escalated')

    def test_not_enough_eligible_jurors_escalates(self):
        other = Community.objects.create(name='Other', slug='other')
        UserProfile.objects.filter(user__in=self.pool).update(community=other)
        d = self.to_jury(self.raise_())
        self.assertEqual(d.status, 'escalated')

    def test_votes_hidden_until_round_closes_via_api(self):
        from rest_framework.test import APIClient
        d = self.to_jury(self.raise_())
        api = APIClient(); api.force_authenticate(self.user1)
        self.vote(d, [100])
        self.assertEqual(api.get(f'/api/v1/disputes/{d.public_id}/').json()['tally'], {'jurors': 3, 'votes_cast': 1})


class AppealTests(DisputeBase, TestCase):
    def decided(self, votes):
        d = self.to_jury(self.raise_())
        self.vote(d, votes)
        return d

    def test_only_losing_side_may_appeal_once(self):
        d = self.decided([100, 100, 100])  # for the taker
        self.assertFalse(disputes.can_appeal(self.user2, d))
        self.assertTrue(disputes.can_appeal(self.user1, d))
        disputes.appeal(d, self.user1)
        d.refresh_from_db()
        self.assertFalse(disputes.can_appeal(self.user1, d))

    def test_successful_appeal_uses_fresh_larger_jury_and_refunds_appeal_bond(self):
        d = self.decided([0, 0, 0])  # against the taker (raiser)
        first = set(d.jurors.values_list('juror_id', flat=True))
        disputes.appeal(d, self.user2)
        d.refresh_from_db()
        self.assertEqual(self.balance(self.user2), 850)  # 50 bond + 100 appeal bond held
        second = set(d.jurors.filter(round=2).values_list('juror_id', flat=True))
        self.assertEqual((d.round, d.stage, len(second)), (2, 'appeal', 7))
        self.assertFalse(first & second)
        self.vote(d, [100] * 7)
        self.assertEqual((d.stage, d.outcome, d.outcome_pct), ('closed', 'appeal', 100))
        self.assertEqual(self.balance(self.user2), 1100)  # reward + both bonds back
        self.assertEqual(p(self.user1), -20)
        self.assertLedgerConsistent()

    def test_failed_appeal_forfeits_both_bonds_to_appeal_jurors(self):
        d = self.decided([0, 0, 0])
        disputes.appeal(d, self.user2)
        d.refresh_from_db()
        self.vote(d, [0] * 7)
        self.assertEqual(self.balance(self.user2), 850)
        rewards = list(JurorAssignment.objects.filter(dispute=d, round=2).values_list('reward', flat=True))
        self.assertEqual(sum(rewards), 147)  # 150 // 7 = 21 each; remainder 3 to system
        self.assertLedgerConsistent()

    def test_appeal_window_closes(self):
        d = self.decided([0, 0, 0])
        Dispute.objects.filter(pk=d.pk).update(appeal_deadline=timezone.now() - timedelta(seconds=1))
        d.refresh_from_db()
        with self.assertRaises(TransitionError):
            disputes.appeal(d, self.user2)


class EligibilityTests(DisputeBase, TestCase):
    def test_excludes_parties_neighbourhood_and_trading_partners(self):
        j0, j1, j2 = self.pool[:3]
        befriend(self.user2, j0)          # friend of the taker
        befriend(j0, j1)                  # friend of a friend
        t = marketplace.post_task(self.user1, title='old', description='', reward=10, wave_policy='community_now')
        marketplace.apply(t, 'accept', j2)  # j2 has traded with the poster
        d = self.raise_()
        eligible = set(disputes.eligible_jurors(d))
        self.assertFalse({self.user1.pk, self.user2.pk, j0.pk, j1.pk, j2.pk} & eligible)
        self.assertIn(self.pool[3].pk, eligible)

    def test_requires_prestige_account_age_and_verification(self):
        low = make_juror('low', prestige=10)
        fresh = make_user('fresh'); UserProfile.objects.filter(user=fresh).update(is_phone_verified=True, prestige=99)
        unverified = make_juror('unverified'); UserProfile.objects.filter(user=unverified).update(is_phone_verified=False)
        eligible = set(disputes.eligible_jurors(self.raise_()))
        self.assertFalse({low.pk, fresh.pk, unverified.pk} & eligible)

    def test_campus_domain_community_counts_as_verified(self):
        campus = Community.objects.create(name='Campus', slug='campus', email_domain='campus.edu')
        UserProfile.objects.update(community=campus, is_phone_verified=False)
        Task.objects.filter(pk=self.task.pk).update(community=campus)
        self.assertTrue(disputes.eligible_jurors(self.raise_()))

    def test_no_show_cooldown(self):
        d = self.to_jury(self.raise_())
        absent = d.jurors.first().juror
        self.expire(d, 'voting_deadline')
        self.assertTrue(PrestigeEvent.objects.filter(user=absent, reason='juror_no_show').exists())
        t2 = marketplace.post_task(make_user('p2'), title='t2', description='', reward=100, wave_policy='community_now')
        taker2 = make_user('t2')
        marketplace.apply(t2, 'accept', taker2)
        d2 = disputes.raise_dispute(t2, taker2, 'again')
        self.assertNotIn(absent.pk, disputes.eligible_jurors(d2))


class ExitTests(DisputeBase, TestCase):
    def test_withdraw_refunds_bond(self):
        d = self.raise_()
        self.c2.post(self.url('withdraw_dispute', d))
        d.refresh_from_db()
        self.assertEqual((d.status, d.outcome), ('withdrawn', 'withdrawn'))
        self.assertEqual(self.refresh().status, Task.Status.IN_PROGRESS)
        self.assertEqual(self.balance(self.user2), 1000)
        self.assertLedgerConsistent()

    def test_cannot_withdraw_after_verdict(self):
        d = self.to_jury(self.raise_())
        self.vote(d, [0, 0, 0])
        with self.assertRaises(TransitionError):
            disputes.withdraw(d, self.user2)

    def test_poster_concession_pays_and_refunds_bond(self):
        d = self.raise_()
        self.c1.post(self.url('complete_task'))
        d.refresh_from_db()
        self.assertEqual((d.outcome, d.outcome_pct), ('conceded', 100))
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()

    def test_resolve_twice_fails_and_non_party_winner_rejected(self):
        d = self.raise_()
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(d, winner=self.user3)
        DisputeService.resolve_dispute(d, winner=self.user2)
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(d, winner=self.user2)

    def test_new_dispute_after_withdraw_keeps_history(self):
        d1 = self.raise_()
        disputes.withdraw(d1, self.user2)
        d2 = self.raise_(reason='again')
        self.assertNotEqual(d1.pk, d2.pk)
        self.assertEqual(self.task.disputes.count(), 2)

    def test_cannot_raise_without_bond_funds(self):
        from ledger import services as ledger
        from ledger.models import Transfer
        ledger.transfer([(ledger.user_account(self.user2), -990), (ledger.system_account(), 990)],
                        key='drain', reason=Transfer.Reason.TASK_FUND)
        self.c2.post(self.url('raise_dispute'), {'reason': 'x'})
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_evidence_rules(self):
        d = self.raise_()
        disputes.add_evidence(d, self.user1, "Never delivered")
        with self.assertRaises(TransitionError):
            disputes.add_evidence(d, self.user3, "I saw it")


class PrivacyTests(DisputeBase, TestCase):
    def test_visible_to_parties_jurors_staff_only(self):
        d = self.to_jury(self.raise_())
        juror = d.jurors.first().juror
        self.assertTrue(disputes.can_view(self.user1, d))
        self.assertTrue(disputes.can_view(juror, d))
        self.assertTrue(disputes.can_view(make_user('staff', staff=True), d))
        nosy = make_user('nosy')
        self.assertFalse(disputes.can_view(nosy, d))
        c = Client(); c.force_login(nosy)
        self.assertRedirects(c.get(reverse('dispute_detail', args=[d.public_id])), reverse('home'),
                             fetch_redirect_response=False)

    def test_jurors_read_chat_only_while_active(self):
        d = self.to_jury(self.raise_())
        conv = Conversation.objects.get(task=self.task)
        juror = d.jurors.first().juror
        self.assertTrue(ChatService.can_read(juror, conv))
        self.assertFalse(ChatService.can_send(juror, conv))
        disputes.withdraw(d, self.user2)
        self.assertFalse(ChatService.can_read(juror, conv))

    def test_full_web_flow(self):
        d = self.raise_()
        self.c1.post(reverse('dispute_offer', args=[d.public_id]), {'taker_pct': 50, 'message': 'meet halfway'})
        self.assertEqual(self.c2.get(reverse('dispute_detail', args=[d.public_id])).status_code, 200)
        self.c2.post(reverse('dispute_accept', args=[d.public_id]))
        d.refresh_from_db()
        self.assertEqual((d.outcome, d.outcome_pct), ('settled', 50))

    def test_web_vote_and_appeal(self):
        d = self.to_jury(self.raise_())
        for seat in d.jurors.select_related('juror'):
            c = Client(); c.force_login(seat.juror)
            self.assertEqual(c.get(reverse('dispute_detail', args=[d.public_id])).status_code, 200)
            c.post(reverse('cast_vote', args=[d.public_id]), {'taker_pct': 0, 'reason': 'No proof of delivery'})
        d.refresh_from_db()
        self.assertEqual(d.stage, 'appeal_window')
        self.c2.post(reverse('dispute_appeal', args=[d.public_id]))
        d.refresh_from_db()
        self.assertEqual((d.round, d.stage), (2, 'appeal'))


class PrestigeTests(World, TestCase):
    def setUp(self):
        self.make_world()

    def test_completion_rewards_taker_and_poster(self):
        marketplace.apply(self.task, 'accept', self.user2)
        marketplace.apply(self.task, 'approve', self.user1)
        self.assertEqual((p(self.user2), p(self.user1)), (10, 3))
        self.assertEqual(prestige.tier(p(self.user2)), 'Newcomer')

    def test_pair_cap_stops_alt_farming(self):
        for i in range(5):
            t = marketplace.post_task(self.user1, title=f't{i}', description='', reward=1, wave_policy='community_now')
            marketplace.apply(t, 'accept', self.user2)
            marketplace.apply(t, 'approve', self.user1)
        self.assertEqual(p(self.user2), 30)  # only 3 counted within 30 days

    def test_abandon_and_auto_approve_cost_prestige(self):
        marketplace.apply(self.task, 'accept', self.user2)
        marketplace.apply(self.task, 'abandon', self.user2)
        self.assertEqual(p(self.user2), -5)
        marketplace.apply(self.task, 'accept', self.user3)
        marketplace.apply(self.task, 'submit', self.user3)
        Task.objects.filter(pk=self.task.pk).update(submitted_at=timezone.now() - timedelta(hours=49))
        marketplace.apply(self.task, 'auto_approve', None)
        self.assertEqual((p(self.user3), p(self.user1)), (10, 3 - 2))

    def test_award_is_idempotent(self):
        prestige.award(self.user1, 'juror_coherent', key='k')
        prestige.award(self.user1, 'juror_coherent', key='k')
        self.assertEqual(p(self.user1), 5)

    def test_tiers(self):
        self.assertEqual([prestige.tier(x) for x in (0, 49, 50, 200, 500)],
                         ['Newcomer', 'Newcomer', 'Trusted', 'Respected', 'Elder'])
