"""
Peer-jury disputes (ARCHITECTURE_V2 §4.5).

1. Raise   - a party opens a dispute while the task is in progress/submitted and posts a bond
             of max(20% of reward, 50 pts) (semantics from PR #167).
2. Evidence- both parties add statements until evidence_deadline.
3. Jury    - up to 5 jurors drawn (seeded, auditable) from the same community, excluding anyone
             within 2 friendship hops of either party and anyone party to an open dispute.
4. Vote    - jurors vote until voting_deadline; votes stay hidden until the dispute closes.
5. Settle  - strict majority with quorum >= 3 decides. Raiser wins -> bond refunded. Raiser loses
             -> bond split between majority jurors (remainder to system). Otherwise escalate to staff.
"""
import math
import random
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ledger import services as ledger
from ledger.models import Transfer
from outbox.services import emit

from ..models import Dispute, DisputeEvidence, Friendship, JurorAssignment, Task, UserProfile
from . import marketplace
from .marketplace import TransitionError


def _cfg():
    return settings.LAZYONE


def bond_for(reward):
    c = _cfg()
    return max(math.ceil(reward * c['BOND_PERCENT'] / 100), c['BOND_MIN'])


# ---------------- policy ----------------
def is_party(user, dispute):
    t = dispute.task
    return user.is_authenticated and user.pk in (t.posted_by_id, t.taken_by_id)


def is_juror(user, dispute):
    return user.is_authenticated and dispute.jurors.filter(juror=user).exists()


def can_view(user, dispute):
    return user.is_authenticated and (user.is_staff or is_party(user, dispute) or is_juror(user, dispute))


def can_withdraw(user, dispute):
    return user.is_authenticated and user.pk == dispute.raised_by_id and dispute.status == Dispute.Status.OPEN


# ---------------- jury selection ----------------
def _neighbourhood(user_ids):
    """User ids within 2 friendship hops of any of `user_ids` (including themselves)."""
    profiles = set(UserProfile.objects.filter(user_id__in=user_ids).values_list('pk', flat=True))
    hop1 = set(Friendship.objects.filter(from_user__in=profiles).values_list('to_user', flat=True))
    hop2 = set(Friendship.objects.filter(from_user__in=hop1).values_list('to_user', flat=True))
    all_profiles = profiles | hop1 | hop2
    return set(UserProfile.objects.filter(pk__in=all_profiles).values_list('user_id', flat=True)) | set(user_ids)


def eligible_jurors(task):
    excluded = _neighbourhood([task.posted_by_id, task.taken_by_id])
    busy = set()
    for t in Task.objects.filter(disputes__status__in=[Dispute.Status.OPEN, Dispute.Status.ESCALATED]):
        busy.update([t.posted_by_id, t.taken_by_id])
    return sorted(
        UserProfile.objects.filter(community=task.community, user__is_active=True)
        .exclude(user_id__in=excluded | busy).values_list('user_id', flat=True)
    )


def draw_jury(dispute):
    candidates = eligible_jurors(dispute.task)
    rng = random.Random(dispute.jury_seed)
    chosen = rng.sample(candidates, k=min(_cfg()['JURY_SIZE'], len(candidates)))
    JurorAssignment.objects.bulk_create([JurorAssignment(dispute=dispute, juror_id=uid) for uid in chosen])
    return chosen


# ---------------- lifecycle ----------------
def raise_dispute(task, user, reason, dispute_type=Dispute.DisputeType.OTHER):
    reason = (reason or '').strip()
    if not reason:
        raise TransitionError("A reason is required to raise a dispute.")
    if dispute_type not in Dispute.DisputeType.values:
        dispute_type = Dispute.DisputeType.OTHER
    c = _cfg()
    with transaction.atomic():
        task = marketplace.apply(task, 'dispute', user, note=reason[:200])
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=task, raised_by=user, reason=reason, dispute_type=dispute_type,
            bond_amount=bond_for(task.reward), jury_seed=secrets.token_hex(8),
            evidence_deadline=now + c['DISPUTE_EVIDENCE_WINDOW'],
            voting_deadline=now + c['DISPUTE_VOTING_WINDOW'],
        )
        try:
            ledger.transfer(
                [(ledger.user_account(user), -dispute.bond_amount), (ledger.dispute_bond(dispute), dispute.bond_amount)],
                key=f"dispute:{dispute.pk}:bond", reason=Transfer.Reason.DISPUTE_BOND, task=task,
                description=f"Dispute bond for '{task.title}'",
            )
        except ledger.InsufficientFunds:
            raise TransitionError(f"Raising a dispute needs a refundable bond of {dispute.bond_amount} points.")
        jurors = draw_jury(dispute)
        if len(jurors) < c['JURY_QUORUM']:
            dispute.status = Dispute.Status.ESCALATED
            dispute.save(update_fields=['status'])
        emit('dispute.opened', {'dispute_id': dispute.pk})
        emit('conversation.members_changed', {'task_id': task.pk})
    return dispute


def add_evidence(dispute, user, text):
    text = (text or '').strip()
    if not text:
        raise TransitionError("Evidence cannot be empty.")
    if not is_party(user, dispute):
        raise TransitionError("Only the people involved in the task can add evidence.")
    if not dispute.is_active or timezone.now() > dispute.evidence_deadline:
        raise TransitionError("The evidence window is closed.")
    return DisputeEvidence.objects.create(dispute=dispute, submitted_by=user, text=text[:4000])


def cast_vote(dispute, user, choice):
    if choice not in JurorAssignment.Vote.values:
        raise TransitionError("Invalid vote.")
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update().get(pk=dispute.pk)
        if dispute.status != Dispute.Status.OPEN or timezone.now() > dispute.voting_deadline:
            raise TransitionError("Voting is closed for this dispute.")
        seat = JurorAssignment.objects.select_for_update().filter(dispute=dispute, juror=user).first()
        if seat is None:
            raise TransitionError("You are not on this jury.")
        if seat.vote:
            raise TransitionError("You have already voted.")
        seat.vote = choice
        seat.voted_at = timezone.now()
        seat.save()
        if not dispute.jurors.filter(vote='').exists():
            tally(dispute)  # everyone voted: settle early
    return seat


def tally(dispute):
    """Count votes and settle or escalate. Called when all jurors voted or the deadline passed."""
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update().get(pk=dispute.pk)
        if dispute.status != Dispute.Status.OPEN:
            return dispute
        votes = list(dispute.jurors.exclude(vote='').values_list('vote', flat=True))
        taker, poster = votes.count('taker'), votes.count('poster')
        if len(votes) < _cfg()['JURY_QUORUM'] or taker == poster:
            dispute.status = Dispute.Status.ESCALATED
            dispute.save(update_fields=['status'])
            emit('dispute.escalated', {'dispute_id': dispute.pk})
            return dispute
        return _settle(dispute, 'taker' if taker > poster else 'poster')


def moderator_resolve(dispute, winner, moderator=None):
    """Staff decision for escalated (or still open) disputes. `winner` is a User."""
    if moderator is not None and not moderator.is_staff:
        raise TransitionError("Only moderators can resolve disputes directly.")
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update(of=('self',)).select_related('task').get(pk=dispute.pk)
        if not dispute.is_active:
            raise ValueError("Can only resolve an open dispute.")
        task = dispute.task
        if winner.pk == task.taken_by_id:
            side = 'taker'
        elif winner.pk == task.posted_by_id:
            side = 'poster'
        else:
            raise ValueError("Winner must be the task creator or the assignee.")
        return _settle(dispute, side, by_jury=False)


def _settle(dispute, side, by_jury=True):
    task = dispute.task
    marketplace.apply(task, 'verdict_taker' if side == 'taker' else 'verdict_poster', None,
                      note=f"dispute {dispute.public_id}")
    raiser_side = 'taker' if dispute.raised_by_id == task.taken_by_id else 'poster'
    bond_acc = ledger.dispute_bond(dispute)
    if dispute.bond_amount:
        if raiser_side == side:
            ledger.transfer([(bond_acc, -dispute.bond_amount), (ledger.user_account(dispute.raised_by), dispute.bond_amount)],
                            key=f"dispute:{dispute.pk}:bond_settle", reason=Transfer.Reason.DISPUTE_BOND_REFUND,
                            task=task, description="Dispute bond refunded (you won)")
        else:
            majority = list(dispute.jurors.filter(vote=side).select_related('juror')) if by_jury else []
            share = dispute.bond_amount // len(majority) if majority else 0
            legs = [(bond_acc, -dispute.bond_amount)]
            for seat in majority:
                legs.append((ledger.user_account(seat.juror), share))
            remainder = dispute.bond_amount - share * len(majority)
            if remainder:
                legs.append((ledger.system_account(), remainder))
            ledger.transfer(legs, key=f"dispute:{dispute.pk}:bond_settle",
                            reason=Transfer.Reason.DISPUTE_BOND_FORFEIT, task=task,
                            description=f"Juror reward for dispute on '{task.title}'")
            for seat in majority:
                seat.reward = share
                seat.save(update_fields=['reward'])
    dispute.status = Dispute.Status.RESOLVED
    dispute.outcome = side
    dispute.resolved_at = timezone.now()
    dispute.save()
    emit('dispute.resolved', {'dispute_id': dispute.pk})
    emit('conversation.members_changed', {'task_id': task.pk})
    return dispute


def withdraw(dispute, user):
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update(of=('self',)).select_related('task').get(pk=dispute.pk)
        if user.pk != dispute.raised_by_id:
            raise TransitionError("You are not authorized to perform this action.")
        if dispute.status != Dispute.Status.OPEN:
            raise TransitionError("This dispute is no longer open.")
        marketplace.apply(dispute.task, 'withdraw_dispute', user)
        _refund_bond(dispute, "Dispute bond refunded (withdrawn)")
        dispute.status = Dispute.Status.WITHDRAWN
        dispute.outcome = Dispute.Outcome.WITHDRAWN
        dispute.resolved_at = timezone.now()
        dispute.save()
        emit('dispute.resolved', {'dispute_id': dispute.pk})
        emit('conversation.members_changed', {'task_id': dispute.task_id})
    return dispute


def close_on_concession(task):
    """Called by marketplace when the poster approves payment during a dispute."""
    dispute = Dispute.objects.select_for_update().filter(
        task=task, status__in=[Dispute.Status.OPEN, Dispute.Status.ESCALATED]).first()
    if not dispute:
        return
    _refund_bond(dispute, "Dispute bond refunded (creator approved payment)")
    dispute.status = Dispute.Status.RESOLVED
    dispute.outcome = Dispute.Outcome.CONCEDED
    dispute.resolved_at = timezone.now()
    dispute.save()
    emit('dispute.resolved', {'dispute_id': dispute.pk})
    emit('conversation.members_changed', {'task_id': task.pk})


def _refund_bond(dispute, description):
    if dispute.bond_amount:
        ledger.transfer(
            [(ledger.dispute_bond(dispute), -dispute.bond_amount), (ledger.user_account(dispute.raised_by), dispute.bond_amount)],
            key=f"dispute:{dispute.pk}:bond_settle", reason=Transfer.Reason.DISPUTE_BOND_REFUND,
            task=dispute.task, description=description,
        )


def close_due_disputes():
    """Tally disputes whose voting window has ended. Returns how many were processed."""
    n = 0
    for d in Dispute.objects.filter(status=Dispute.Status.OPEN, voting_deadline__lte=timezone.now()):
        tally(d)
        n += 1
    return n
