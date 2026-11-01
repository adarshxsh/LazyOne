"""
Dispute resolution v2 (docs/Ideation/DISPUTE_RESOLUTION.md).

    1. SETTLEMENT  raiser opens with an offer (taker's share of the reward, %); the other side
                   accepts, counters or rejects. No response by the deadline -> raiser's offer wins.
    2. JURY        size by reward (1 / 3 / 5); eligible jurors only (verified, account age,
                   prestige, no ties to either party). Vote = 0 / 50 / 100 + required reason.
                   Result = median vote.
    3. APPEAL      the side the verdict went against may appeal once (double bond):
                   fresh, larger jury (2n+1); its result is final.
    4. MODERATOR   no quorum or a tie -> staff decide.

Prestige matters more than points: losing costs -20 prestige for either side, coherent jurors
earn +5, no-shows -5 and a cooldown. Points: the raiser's bond (and an appeal bond) is refunded
or shared by the coherent jurors of the deciding round.
"""
import math
import random
import secrets
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ledger import services as ledger
from ledger.models import Transfer
from outbox.services import emit

from ..models import (Dispute, DisputeEvidence, Friendship, JurorAssignment, PrestigeEvent, SettlementOffer,
                      Task, UserProfile)
from . import marketplace, prestige
from .marketplace import TransitionError

VOTE_VALUES = (0, 50, 100)
ACTIVE = (Dispute.Status.OPEN, Dispute.Status.ESCALATED)


def _cfg():
    return settings.LAZYONE


def bond_for(reward):
    c = _cfg()
    return max(math.ceil(reward * c['BOND_PERCENT'] / 100), c['BOND_MIN'])


def jury_size_for(reward):
    for limit, size in _cfg()['JURY_TIERS']:
        if limit is None or reward < limit:
            return size
    return _cfg()['JURY_TIERS'][-1][1]


def quorum_for(size):
    return size // 2 + 1


# ------------------------------------------------------------------ helpers

def _side(dispute, user):
    t = dispute.task
    if user.pk == t.taken_by_id:
        return 'taker'
    if user.pk == t.posted_by_id:
        return 'poster'
    return None


def _against(pct, side):
    """Did an outcome of `pct` (taker's share) go against `side`?"""
    return (side == 'taker' and pct < 50) or (side == 'poster' and pct > 50)


def _locked(dispute):
    return Dispute.objects.select_for_update(of=('self',)).select_related(
        'task__posted_by', 'task__taken_by', 'raised_by').get(pk=dispute.pk)


# ------------------------------------------------------------------ policy

def is_party(user, dispute):
    return user.is_authenticated and _side(dispute, user) is not None


def is_juror(user, dispute):
    return user.is_authenticated and dispute.jurors.filter(juror=user).exists()


def can_view(user, dispute):
    return user.is_authenticated and (user.is_staff or is_party(user, dispute) or is_juror(user, dispute))


def can_withdraw(user, dispute):
    return (user.is_authenticated and user.pk == dispute.raised_by_id and dispute.status == Dispute.Status.OPEN
            and dispute.stage in (Dispute.Stage.SETTLEMENT, Dispute.Stage.JURY))


def can_appeal(user, dispute):
    if not user.is_authenticated or dispute.stage != Dispute.Stage.APPEAL_WINDOW:
        return False
    if dispute.appeal_deadline and timezone.now() > dispute.appeal_deadline:
        return False
    side = _side(dispute, user)
    return side is not None and (dispute.outcome_pct == 50 or _against(dispute.outcome_pct, side))


# ------------------------------------------------------------------ jury selection

def _neighbourhood(user_ids):
    profiles = set(UserProfile.objects.filter(user_id__in=user_ids).values_list('pk', flat=True))
    hop1 = set(Friendship.objects.filter(from_user__in=profiles).values_list('to_user', flat=True))
    hop2 = set(Friendship.objects.filter(from_user__in=hop1).values_list('to_user', flat=True))
    return set(UserProfile.objects.filter(pk__in=profiles | hop1 | hop2).values_list('user_id', flat=True)) | set(user_ids)


def _transacted_with(user_ids):
    """Anyone who ever posted or took a task with either party."""
    rows = Task.objects.filter(Q(posted_by_id__in=user_ids) | Q(taken_by_id__in=user_ids)).values_list(
        'posted_by_id', 'taken_by_id')
    return {uid for pair in rows for uid in pair if uid}


def eligible_jurors(dispute, exclude=()):
    c, task, now = _cfg(), dispute.task, timezone.now()
    parties = [task.posted_by_id, task.taken_by_id]
    excluded = _neighbourhood(parties) | _transacted_with(parties) | set(exclude)
    busy = set()
    for t in Task.objects.filter(disputes__status__in=ACTIVE):
        busy.update([t.posted_by_id, t.taken_by_id])
    cooldown = set(PrestigeEvent.objects.filter(
        reason='juror_no_show', created_at__gte=now - c['JUROR_NO_SHOW_COOLDOWN']).values_list('user_id', flat=True))

    qs = UserProfile.objects.filter(community=task.community, user__is_active=True,
                                    prestige__gte=c['JUROR_MIN_PRESTIGE'],
                                    user__date_joined__lte=now - c['JUROR_MIN_ACCOUNT_AGE'])
    if c['JUROR_REQUIRE_VERIFIED'] and not (task.community and task.community.email_domain):
        qs = qs.filter(is_phone_verified=True)  # campus-domain communities are verified by email
    return sorted(qs.exclude(user_id__in=excluded | busy | cooldown).values_list('user_id', flat=True))


def _draw(dispute, size, exclude=()):
    candidates = eligible_jurors(dispute, exclude)
    rng = random.Random(f"{dispute.jury_seed}:{dispute.round}")
    chosen = rng.sample(candidates, k=min(size, len(candidates)))
    JurorAssignment.objects.bulk_create(
        [JurorAssignment(dispute=dispute, juror_id=uid, round=dispute.round) for uid in chosen])
    return chosen


def draw_jury(dispute):
    """Round-1 jury; kept as a public helper for audits (same seed -> same jurors)."""
    return _draw(dispute, jury_size_for(dispute.task.reward))


# ------------------------------------------------------------------ 1. raise + settlement

def raise_dispute(task, user, reason, dispute_type=Dispute.DisputeType.OTHER, taker_pct=None):
    """Open a dispute with the raiser's first offer (defaults to fully in the raiser's favour)."""
    reason = (reason or '').strip()
    if not reason:
        raise TransitionError("A reason is required to raise a dispute.")
    if dispute_type not in Dispute.DisputeType.values:
        dispute_type = Dispute.DisputeType.OTHER
    with transaction.atomic():
        task = marketplace.apply(task, 'dispute', user, note=reason[:200])
        side = 'taker' if user.pk == task.taken_by_id else 'poster'
        if taker_pct is None:
            taker_pct = 100 if side == 'taker' else 0
        taker_pct = _valid_pct(taker_pct)
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=task, raised_by=user, reason=reason, dispute_type=dispute_type,
            bond_amount=bond_for(task.reward), jury_seed=secrets.token_hex(8),
            stage=Dispute.Stage.SETTLEMENT, settlement_deadline=now + _cfg()['DISPUTE_SETTLEMENT_WINDOW'],
            evidence_deadline=now + _cfg()['DISPUTE_SETTLEMENT_WINDOW'] + _cfg()['DISPUTE_EVIDENCE_WINDOW'],
        )
        try:
            ledger.transfer(
                [(ledger.user_account(user), -dispute.bond_amount), (ledger.dispute_bond(dispute), dispute.bond_amount)],
                key=f"dispute:{dispute.pk}:bond", reason=Transfer.Reason.DISPUTE_BOND, task=task,
                description=f"Dispute bond for '{task.title}'",
            )
        except ledger.InsufficientFunds:
            raise TransitionError(f"Raising a dispute needs a refundable bond of {dispute.bond_amount} points.")
        SettlementOffer.objects.create(dispute=dispute, proposed_by=user, taker_pct=taker_pct,
                                       message="Opening offer")
        emit('dispute.opened', {'dispute_id': dispute.pk})
    return dispute


def _valid_pct(pct):
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        raise TransitionError("Choose how much of the reward the taker should get (0-100 %).")
    if not 0 <= pct <= 100:
        raise TransitionError("The taker's share must be between 0 and 100 %.")
    return pct


def _require_settlement(dispute, user):
    if dispute.status != Dispute.Status.OPEN or dispute.stage != Dispute.Stage.SETTLEMENT:
        raise TransitionError("The settlement stage is over.")
    if timezone.now() > dispute.settlement_deadline:
        raise TransitionError("The settlement deadline has passed.")
    if not is_party(user, dispute):
        raise TransitionError("Only the people involved in the task can negotiate.")


def make_offer(dispute, user, taker_pct, message=''):
    with transaction.atomic():
        dispute = _locked(dispute)
        _require_settlement(dispute, user)
        dispute.offers.filter(status=SettlementOffer.Status.PENDING).update(status=SettlementOffer.Status.SUPERSEDED)
        offer = SettlementOffer.objects.create(dispute=dispute, proposed_by=user, taker_pct=_valid_pct(taker_pct),
                                               message=(message or '')[:500])
        emit('dispute.offer', {'dispute_id': dispute.pk, 'offer_id': offer.pk})
    return offer


def accept_offer(dispute, user):
    with transaction.atomic():
        dispute = _locked(dispute)
        _require_settlement(dispute, user)
        offer = dispute.offers.filter(status=SettlementOffer.Status.PENDING).last()
        if offer is None:
            raise TransitionError("There is no offer to accept.")
        if offer.proposed_by_id == user.pk:
            raise TransitionError("You can't accept your own offer.")
        offer.status = SettlementOffer.Status.ACCEPTED
        offer.save(update_fields=['status'])
        _finalize(dispute, offer.taker_pct, Dispute.Outcome.SETTLED)
        for uid in (dispute.task.posted_by, dispute.task.taken_by):
            other = dispute.task.taken_by if uid == dispute.task.posted_by else dispute.task.posted_by
            prestige.award(uid, 'dispute_settled', key=f"dispute:{dispute.pk}:settled:{uid.pk}",
                           dispute=dispute, counterparty=other)
    return dispute


def reject_offers(dispute, user):
    """Refuse to settle: go straight to the jury."""
    with transaction.atomic():
        dispute = _locked(dispute)
        _require_settlement(dispute, user)
        dispute.offers.filter(status=SettlementOffer.Status.PENDING).update(status=SettlementOffer.Status.REJECTED)
        _start_jury(dispute)
    return dispute


def _respondent_engaged(dispute):
    respondent_id = dispute.task.posted_by_id if dispute.raised_by_id == dispute.task.taken_by_id else dispute.task.taken_by_id
    return (dispute.offers.filter(proposed_by_id=respondent_id).exists()
            or dispute.evidence.filter(submitted_by_id=respondent_id).exists())


def _end_settlement(dispute):
    """Settlement deadline passed: default judgment if the other side ignored it, else jury."""
    if _respondent_engaged(dispute):
        dispute.offers.filter(status=SettlementOffer.Status.PENDING).update(status=SettlementOffer.Status.REJECTED)
        _start_jury(dispute)
        return
    opening = dispute.offers.filter(proposed_by=dispute.raised_by).last()
    respondent = dispute.task.posted_by if dispute.raised_by_id == dispute.task.taken_by_id else dispute.task.taken_by
    _finalize(dispute, opening.taker_pct, Dispute.Outcome.DEFAULT)
    prestige.award(respondent, 'dispute_default', key=f"dispute:{dispute.pk}:default", dispute=dispute)


# ------------------------------------------------------------------ 2. jury

def _start_jury(dispute):
    now = timezone.now()
    size = jury_size_for(dispute.task.reward) if dispute.round == 1 else 2 * jury_size_for(dispute.task.reward) + 1
    previous = list(dispute.jurors.values_list('juror_id', flat=True))
    dispute.stage = Dispute.Stage.JURY if dispute.round == 1 else Dispute.Stage.APPEAL
    dispute.evidence_deadline = max(dispute.evidence_deadline or now, now + _cfg()['DISPUTE_EVIDENCE_WINDOW'])
    dispute.voting_deadline = now + _cfg()['DISPUTE_VOTING_WINDOW']
    dispute.save()
    chosen = _draw(dispute, size, exclude=previous)
    if len(chosen) < quorum_for(size):
        _escalate(dispute)
        return
    emit('dispute.jury', {'dispute_id': dispute.pk, 'round': dispute.round})
    emit('conversation.members_changed', {'task_id': dispute.task_id})


def add_evidence(dispute, user, text):
    text = (text or '').strip()
    if not text:
        raise TransitionError("Evidence cannot be empty.")
    if not is_party(user, dispute):
        raise TransitionError("Only the people involved in the task can add evidence.")
    if not dispute.is_active or (dispute.evidence_deadline and timezone.now() > dispute.evidence_deadline):
        raise TransitionError("The evidence window is closed.")
    return DisputeEvidence.objects.create(dispute=dispute, submitted_by=user, text=text[:4000])


def cast_vote(dispute, user, taker_pct, reason=''):
    reason = (reason or '').strip()
    if len(reason) < 10:
        raise TransitionError("Please give a short reason for your vote (at least 10 characters).")
    try:
        taker_pct = int(taker_pct)
    except (TypeError, ValueError):
        taker_pct = None
    if taker_pct not in VOTE_VALUES:
        raise TransitionError("Invalid vote.")
    with transaction.atomic():
        dispute = _locked(dispute)
        if dispute.status != Dispute.Status.OPEN or dispute.stage not in (Dispute.Stage.JURY, Dispute.Stage.APPEAL) \
                or timezone.now() > dispute.voting_deadline:
            raise TransitionError("Voting is closed for this dispute.")
        seat = JurorAssignment.objects.select_for_update().filter(dispute=dispute, juror=user,
                                                                  round=dispute.round).first()
        if seat is None:
            raise TransitionError("You are not on this jury.")
        if seat.has_voted:
            raise TransitionError("You have already voted.")
        seat.taker_pct, seat.reason, seat.voted_at = taker_pct, reason[:500], timezone.now()
        seat.save()
        if not dispute.jurors.filter(round=dispute.round, taker_pct__isnull=True).exists():
            _tally(dispute)  # everyone voted: settle early
    return seat


def _median(values):
    v = sorted(values)
    n = len(v)
    if n % 2:
        return v[n // 2]
    a, b = v[n // 2 - 1], v[n // 2]
    return a if a == b else None  # even split between different outcomes -> no clear result


def tally(dispute):
    with transaction.atomic():
        return _tally(_locked(dispute))


def _tally(dispute):
    if dispute.status != Dispute.Status.OPEN or dispute.stage not in (Dispute.Stage.JURY, Dispute.Stage.APPEAL):
        return dispute
    seats = list(dispute.jurors.filter(round=dispute.round))
    votes = [s.taker_pct for s in seats if s.has_voted]
    size = len(seats)
    result = _median(votes) if len(votes) >= quorum_for(size) else None
    _score_jurors(dispute, seats, result)
    if result is None:
        _escalate(dispute)
        return dispute
    if dispute.round == 1:
        dispute.stage = Dispute.Stage.APPEAL_WINDOW
        dispute.outcome_pct = dispute.round1_pct = result
        dispute.appeal_deadline = timezone.now() + _cfg()['DISPUTE_APPEAL_WINDOW']
        dispute.save()
        emit('dispute.decided', {'dispute_id': dispute.pk})
    else:
        _finalize(dispute, result, Dispute.Outcome.APPEAL)
    return dispute


def _score_jurors(dispute, seats, result):
    for seat in seats:
        if not seat.has_voted:
            prestige.award(seat.juror, 'juror_no_show', key=f"dispute:{dispute.pk}:noshow:{seat.juror_id}",
                           dispute=dispute)
            continue
        seat.coherent = result is not None and seat.taker_pct == result
        seat.save(update_fields=['coherent'])
        if seat.coherent:
            prestige.award(seat.juror, 'juror_coherent', key=f"dispute:{dispute.pk}:coherent:{seat.juror_id}",
                           dispute=dispute)


def _escalate(dispute):
    dispute.status = Dispute.Status.ESCALATED
    dispute.save(update_fields=['status'])
    emit('dispute.escalated', {'dispute_id': dispute.pk})


# ------------------------------------------------------------------ 3. appeal

def appeal(dispute, user):
    with transaction.atomic():
        dispute = _locked(dispute)
        if not can_appeal(user, dispute):
            raise TransitionError("This verdict can't be appealed by you (or the appeal window has closed).")
        dispute.appeal_bond = 2 * dispute.bond_amount
        try:
            ledger.transfer(
                [(ledger.user_account(user), -dispute.appeal_bond), (ledger.dispute_bond(dispute), dispute.appeal_bond)],
                key=f"dispute:{dispute.pk}:appeal_bond", reason=Transfer.Reason.DISPUTE_BOND, task=dispute.task,
                description=f"Appeal bond for '{dispute.task.title}'",
            )
        except ledger.InsufficientFunds:
            raise TransitionError(f"An appeal needs a refundable bond of {dispute.appeal_bond} points.")
        dispute.appealed_by = user
        dispute.round = 2
        dispute.save()
        _start_jury(dispute)
        emit('dispute.appealed', {'dispute_id': dispute.pk})
    return dispute


# ------------------------------------------------------------------ 4. moderator, withdraw, concede

def moderator_resolve(dispute, winner=None, moderator=None, taker_pct=None):
    """Staff decision. Pass `taker_pct` (0-100) or `winner` (a party, for 0/100)."""
    if moderator is not None and not moderator.is_staff:
        raise TransitionError("Only moderators can resolve disputes directly.")
    with transaction.atomic():
        dispute = _locked(dispute)
        if not dispute.is_active:
            raise ValueError("Can only resolve an open dispute.")
        if taker_pct is None:
            side = _side(dispute, winner) if winner is not None else None
            if side is None:
                raise ValueError("Winner must be the task creator or the assignee.")
            taker_pct = 100 if side == 'taker' else 0
        return _finalize(dispute, _valid_pct(taker_pct), Dispute.Outcome.MODERATOR)


def withdraw(dispute, user):
    with transaction.atomic():
        dispute = _locked(dispute)
        if user.pk != dispute.raised_by_id:
            raise TransitionError("You are not authorized to perform this action.")
        if not can_withdraw(user, dispute):
            raise TransitionError("This dispute can no longer be withdrawn.")
        marketplace.apply(dispute.task, 'withdraw_dispute', user)
        _close(dispute, Dispute.Outcome.WITHDRAWN, None, refund_all=True)
        emit('conversation.members_changed', {'task_id': dispute.task_id})
    return dispute


def close_on_concession(task):
    """Poster approved full payment while disputed (called by marketplace inside the transaction)."""
    dispute = Dispute.objects.select_for_update(of=('self',)).filter(task=task, status__in=ACTIVE).first()
    if dispute:
        _close(dispute, Dispute.Outcome.CONCEDED, 100, refund_all=True)
        emit('conversation.members_changed', {'task_id': task.pk})


# ------------------------------------------------------------------ settlement of money + prestige

def _finalize(dispute, taker_pct, outcome):
    """Execute an outcome: move the reward, settle bonds, apply loser prestige, close."""
    task = dispute.task
    action = 'verdict_taker' if taker_pct == 100 else 'verdict_poster' if taker_pct == 0 else 'verdict_split'
    marketplace.apply(task, action, None, note=f"dispute {dispute.public_id}: taker {taker_pct}%", taker_pct=taker_pct)
    if outcome in (Dispute.Outcome.SETTLED, Dispute.Outcome.DEFAULT):
        # Agreed between the parties, or the other side never showed up: no judges, bonds refunded.
        _close(dispute, outcome, taker_pct, refund_all=True)
        return dispute
    _settle_bonds(dispute, taker_pct)
    if taker_pct != 50:
        loser = task.taken_by if taker_pct < 50 else task.posted_by
        prestige.award(loser, 'dispute_lost', key=f"dispute:{dispute.pk}:lost", dispute=dispute)
    _close(dispute, outcome, taker_pct, refund_all=False)
    return dispute


def _settle_bonds(dispute, taker_pct):
    """Raiser bond: refunded unless the outcome went against the raiser; then shared by coherent jurors
    of the deciding round. Appeal bond: refunded if the appeal improved the appellant's result."""
    task = dispute.task
    raiser_side = _side(dispute, dispute.raised_by)
    legs, total = [], 0
    final_round = dispute.round
    coherent = list(dispute.jurors.filter(round=final_round, coherent=True).select_related('juror'))

    def pay_out(amount, to_user=None):
        if amount <= 0:
            return
        if to_user is not None:
            legs.append((ledger.user_account(to_user), amount))
        elif coherent:
            share = amount // len(coherent)
            for seat in coherent:
                legs.append((ledger.user_account(seat.juror), share))
                seat.reward += share
                seat.save(update_fields=['reward'])
            rest = amount - share * len(coherent)
            if rest:
                legs.append((ledger.system_account(), rest))
        else:
            legs.append((ledger.system_account(), amount))

    if dispute.bond_amount:
        total += dispute.bond_amount
        pay_out(dispute.bond_amount, None if _against(taker_pct, raiser_side) else dispute.raised_by)
    if dispute.appeal_bond and dispute.appealed_by_id:
        total += dispute.appeal_bond
        side = _side(dispute, dispute.appealed_by)
        improved = (taker_pct > dispute.round1_pct) if side == 'taker' else (taker_pct < dispute.round1_pct)
        pay_out(dispute.appeal_bond, dispute.appealed_by if improved else None)
    if total:
        ledger.transfer([(ledger.dispute_bond(dispute), -total)] + legs, key=f"dispute:{dispute.pk}:bond_settle",
                        reason=Transfer.Reason.DISPUTE_BOND_FORFEIT, task=task,
                        description=f"Dispute bonds settled for '{task.title}'")


def _close(dispute, outcome, taker_pct, refund_all):
    if refund_all:
        total = dispute.bond_amount + (dispute.appeal_bond if dispute.appealed_by_id else 0)
        if total:
            legs = [(ledger.dispute_bond(dispute), -total)]
            if dispute.bond_amount:
                legs.append((ledger.user_account(dispute.raised_by), dispute.bond_amount))
            if dispute.appealed_by_id and dispute.appeal_bond:
                legs.append((ledger.user_account(dispute.appealed_by), dispute.appeal_bond))
            ledger.transfer(legs, key=f"dispute:{dispute.pk}:bond_settle", reason=Transfer.Reason.DISPUTE_BOND_REFUND,
                            task=dispute.task, description="Dispute bonds refunded")
        dispute.offers.filter(status=SettlementOffer.Status.PENDING).update(status=SettlementOffer.Status.SUPERSEDED)
    dispute.status = Dispute.Status.WITHDRAWN if outcome == Dispute.Outcome.WITHDRAWN else Dispute.Status.RESOLVED
    dispute.stage = Dispute.Stage.CLOSED
    dispute.outcome = outcome
    dispute.outcome_pct = taker_pct
    dispute.resolved_at = timezone.now()
    dispute.save()
    emit('dispute.resolved', {'dispute_id': dispute.pk})


# ------------------------------------------------------------------ timers

def close_due_disputes():
    """Advance disputes whose stage deadline passed. Returns how many were processed."""
    now, n = timezone.now(), 0
    due = Dispute.objects.filter(status=Dispute.Status.OPEN).filter(
        Q(stage=Dispute.Stage.SETTLEMENT, settlement_deadline__lte=now)
        | Q(stage__in=[Dispute.Stage.JURY, Dispute.Stage.APPEAL], voting_deadline__lte=now)
        | Q(stage=Dispute.Stage.APPEAL_WINDOW, appeal_deadline__lte=now))
    for pk in due.values_list('pk', flat=True):
        with transaction.atomic():
            d = _locked(Dispute(pk=pk))
            if d.status != Dispute.Status.OPEN:
                continue
            if d.stage == Dispute.Stage.SETTLEMENT:
                _end_settlement(d)
            elif d.stage == Dispute.Stage.APPEAL_WINDOW:
                _finalize(d, d.outcome_pct, Dispute.Outcome.JURY)
            else:
                _tally(d)
        n += 1
    return n

