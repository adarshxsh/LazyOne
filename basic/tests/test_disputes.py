from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Community, Conversation, Dispute, JurorAssignment, Task
from ..services import disputes, marketplace
from ..services.marketplace import TransitionError
from ..services.permissions import ChatService, DisputeService
from .base import World, befriend, make_user


class DisputeBase(World):
    def setUp(self):
        self.make_world()
        self.jurors = [make_user(f'j{i}') for i in range(5)]
        self.c2.post(self.url('take_task'))

    def raise_dispute(self, client=None, reason='Payment delayed'):
        (client or self.c2).post(self.url('raise_dispute'), {'reason': reason})
        return Dispute.objects.filter(task=self.task).latest('created_at')

    def vote_all(self, dispute, votes):
        seats = list(dispute.jurors.select_related('juror'))
        for seat, v in zip(seats, votes):
            disputes.cast_vote(dispute, seat.juror, v)
        dispute.refresh_from_db()
        return dispute


class DisputeLifecycleTests(DisputeBase, TestCase):
    def test_raise_takes_bond_and_draws_jury(self):
        d = self.raise_dispute()
        self.assertEqual(self.refresh().status, Task.Status.DISPUTED)
        self.assertEqual(d.bond_amount, 50)  # max(20% of 100, 50)
        self.assertEqual(self.balance(self.user2), 950)
        self.assertEqual(d.jurors.count(), 5)
        self.assertEqual(d.status, Dispute.Status.OPEN)
        self.assertLedgerConsistent()

    def test_bond_scales_with_reward(self):
        self.assertEqual(disputes.bond_for(1000), 200)
        self.assertEqual(disputes.bond_for(10), 50)

    def test_cannot_raise_without_bond_funds(self):
        from ledger import services as ledger
        from ledger.models import Transfer
        ledger.transfer([(ledger.user_account(self.user2), -990), (ledger.system_account(), 990)],
                        key='drain', reason=Transfer.Reason.TASK_FUND)
        self.c2.post(self.url('raise_dispute'), {'reason': 'x'})
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertEqual(self.refresh().status, Task.Status.IN_PROGRESS)

    def test_outsider_cannot_raise(self):
        self.c3.post(self.url('raise_dispute'), {'reason': 'x'})
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_jury_excludes_parties_and_two_hop_neighbourhood(self):
        befriend(self.user2, self.jurors[0])           # 1 hop from taker
        befriend(self.jurors[0], self.jurors[1])       # 2 hops from taker
        d = self.raise_dispute()
        chosen = set(d.jurors.values_list('juror_id', flat=True))
        self.assertNotIn(self.user1.pk, chosen)
        self.assertNotIn(self.user2.pk, chosen)
        self.assertNotIn(self.jurors[0].pk, chosen)
        self.assertNotIn(self.jurors[1].pk, chosen)

    def test_jury_limited_to_same_community(self):
        other = Community.objects.create(name='Other', slug='other')
        for j in self.jurors:
            j.userprofile.community = other
            j.userprofile.save()
        d = self.raise_dispute()
        self.assertEqual(d.status, Dispute.Status.ESCALATED)  # only user3 is eligible

    def test_jury_draw_is_reproducible_from_seed(self):
        d = self.raise_dispute()
        first = sorted(d.jurors.values_list('juror_id', flat=True))
        JurorAssignment.objects.filter(dispute=d).delete()
        self.assertEqual(sorted(disputes.draw_jury(d)), first)

    def test_majority_for_raiser_refunds_bond_and_pays(self):
        d = self.vote_all(self.raise_dispute(), ['taker', 'taker', 'taker', 'poster', 'poster'])
        self.assertEqual(d.status, Dispute.Status.RESOLVED)
        self.assertEqual(d.outcome, 'taker')
        self.assertEqual(self.refresh().status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()

    def test_majority_against_raiser_splits_bond_to_majority(self):
        d = self.vote_all(self.raise_dispute(), ['poster', 'poster', 'poster', 'taker', 'taker'])
        self.assertEqual(d.outcome, 'poster')
        self.assertEqual(self.refresh().status, Task.Status.CANCELLED)
        self.assertEqual(self.balance(self.user1), 1000)   # refunded
        self.assertEqual(self.balance(self.user2), 950)    # lost bond
        rewards = sorted(d.jurors.values_list('reward', flat=True))
        self.assertEqual(rewards, [0, 0, 16, 16, 16])      # 50 // 3, remainder to system
        self.assertLedgerConsistent()

    def test_vote_rules(self):
        d = self.raise_dispute()
        seat = d.jurors.first()
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, make_user('notjuror'), 'taker')
        disputes.cast_vote(d, seat.juror, 'taker')
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, seat.juror, 'poster')
        with self.assertRaises(TransitionError):
            disputes.cast_vote(d, d.jurors.exclude(pk=seat.pk).first().juror, 'nonsense')

    def test_deadline_without_quorum_escalates(self):
        d = self.raise_dispute()
        disputes.cast_vote(d, d.jurors.first().juror, 'taker')
        Dispute.objects.filter(pk=d.pk).update(voting_deadline=timezone.now() - timedelta(minutes=1))
        disputes.close_due_disputes()
        d.refresh_from_db()
        self.assertEqual(d.status, Dispute.Status.ESCALATED)
        self.assertEqual(self.refresh().status, Task.Status.DISPUTED)

    def test_tie_escalates_then_moderator_resolves(self):
        d = self.raise_dispute()
        seats = list(d.jurors.select_related('juror'))
        for seat, v in zip(seats[:4], ['taker', 'taker', 'poster', 'poster']):
            disputes.cast_vote(d, seat.juror, v)
        Dispute.objects.filter(pk=d.pk).update(voting_deadline=timezone.now() - timedelta(minutes=1))
        disputes.close_due_disputes()
        d.refresh_from_db()
        self.assertEqual(d.status, Dispute.Status.ESCALATED)
        mod = make_user('mod', staff=True)
        DisputeService.resolve_dispute(d, winner=self.user1, moderator=mod)
        d.refresh_from_db()
        self.assertEqual(d.outcome, 'poster')
        self.assertLedgerConsistent()

    def test_non_staff_cannot_moderate(self):
        d = self.raise_dispute()
        with self.assertRaises(TransitionError):
            disputes.moderator_resolve(d, self.user2, moderator=self.user3)

    def test_withdraw_refunds_bond(self):
        d = self.raise_dispute()
        self.c2.post(self.url('withdraw_dispute', d))
        d.refresh_from_db()
        self.assertEqual(d.status, Dispute.Status.WITHDRAWN)
        self.assertEqual(self.refresh().status, Task.Status.IN_PROGRESS)
        self.assertEqual(self.balance(self.user2), 1000)
        self.assertLedgerConsistent()

    def test_cannot_withdraw_resolved_dispute(self):
        d = self.raise_dispute()
        DisputeService.resolve_dispute(d, winner=self.user2)
        self.c2.post(self.url('withdraw_dispute', d))
        self.c1.post(self.url('complete_task'))
        self.assertEqual(self.refresh().status, Task.Status.COMPLETED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()

    def test_resolve_twice_fails(self):
        d = self.raise_dispute()
        DisputeService.resolve_dispute(d, winner=self.user2)
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(d, winner=self.user2)

    def test_resolve_rejects_non_party_winner(self):
        d = self.raise_dispute()
        with self.assertRaises(ValueError):
            DisputeService.resolve_dispute(d, winner=self.user3)

    def test_poster_concession_closes_dispute_and_refunds_bond(self):
        d = self.raise_dispute()
        self.c1.post(self.url('complete_task'))
        d.refresh_from_db()
        self.assertEqual(d.outcome, Dispute.Outcome.CONCEDED)
        self.assertEqual(self.balance(self.user2), 1100)
        self.assertLedgerConsistent()

    def test_new_dispute_after_withdraw_keeps_history(self):
        d1 = self.raise_dispute()
        disputes.withdraw(d1, self.user2)
        d2 = self.raise_dispute(reason='again')
        self.assertNotEqual(d1.pk, d2.pk)
        self.assertEqual(self.task.disputes.count(), 2)

    def test_evidence_only_from_parties_within_window(self):
        d = self.raise_dispute()
        disputes.add_evidence(d, self.user1, "Never delivered")
        with self.assertRaises(TransitionError):
            disputes.add_evidence(d, self.user3, "I saw it")
        Dispute.objects.filter(pk=d.pk).update(evidence_deadline=timezone.now() - timedelta(seconds=1))
        d.refresh_from_db()
        with self.assertRaises(TransitionError):
            disputes.add_evidence(d, self.user2, "late")


class DisputePrivacyTests(DisputeBase, TestCase):
    def test_dispute_visible_to_parties_jurors_staff_only(self):
        d = self.raise_dispute()
        juror = d.jurors.first().juror
        outsider = make_user('nosy')
        self.assertTrue(disputes.can_view(self.user1, d))
        self.assertTrue(disputes.can_view(juror, d))
        self.assertTrue(disputes.can_view(make_user('staff', staff=True), d))
        self.assertFalse(disputes.can_view(outsider, d))
        from django.test import Client
        c = Client(); c.force_login(outsider)
        self.assertRedirects(c.get(reverse('dispute_detail', args=[d.public_id])), reverse('home'),
                             fetch_redirect_response=False)

    def test_disputed_chat_readable_by_jurors_not_public(self):
        d = self.raise_dispute()
        conv = Conversation.objects.get(task=self.task)
        juror = d.jurors.first().juror
        self.assertTrue(ChatService.can_read(juror, conv))
        self.assertFalse(ChatService.can_send(juror, conv))
        self.assertFalse(ChatService.can_read(make_user('nosy'), conv))
        self.assertIn(juror.pk, ChatService.reader_ids(conv))
        disputes.withdraw(d, self.user2)
        self.assertFalse(ChatService.can_read(juror, conv))

    def test_juror_votes_via_web(self):
        d = self.raise_dispute()
        seat = d.jurors.select_related('juror').first()
        from django.test import Client
        c = Client(); c.force_login(seat.juror)
        self.assertEqual(c.get(reverse('dispute_detail', args=[d.public_id])).status_code, 200)
        c.post(reverse('cast_vote', args=[d.public_id]), {'vote': 'taker'})
        seat.refresh_from_db()
        self.assertEqual(seat.vote, 'taker')
