from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Dispute, JurorAssignment
from ..services import disputes, prestige
from ..services.marketplace import TransitionError
from ..ratelimit import ratelimit


def _get(public_id):
    return get_object_or_404(Dispute.objects.select_related('task__posted_by', 'task__taken_by', 'raised_by'),
                             public_id=public_id)


@login_required(login_url='/login/')
def dispute_detail_view(request, public_id):
    dispute = _get(public_id)
    if not disputes.can_view(request.user, dispute):
        messages.error(request, "Only the people involved, the jury and moderators can view this dispute.")
        return redirect('home')
    seat = dispute.jurors.filter(juror=request.user).order_by('-round').first()
    round_seats = dispute.jurors.filter(round=dispute.round)
    closed_round = dispute.stage in (Dispute.Stage.APPEAL_WINDOW, Dispute.Stage.CLOSED) or not dispute.is_active
    pending = dispute.offers.filter(status='pending').select_related('proposed_by').last()
    context = {
        'dispute': dispute,
        'task': dispute.task,
        'evidence': dispute.evidence.select_related('submitted_by'),
        'offers': dispute.offers.select_related('proposed_by'),
        'pending_offer': pending,
        'can_accept_offer': bool(pending and pending.proposed_by_id != request.user.id
                                 and disputes.is_party(request.user, dispute)
                                 and dispute.stage == Dispute.Stage.SETTLEMENT),
        'is_party': disputes.is_party(request.user, dispute),
        'seat': seat,
        'can_vote': bool(seat and seat.round == dispute.round and not seat.has_voted
                         and dispute.status == Dispute.Status.OPEN
                         and dispute.stage in (Dispute.Stage.JURY, Dispute.Stage.APPEAL)),
        'can_withdraw': disputes.can_withdraw(request.user, dispute),
        'can_appeal': disputes.can_appeal(request.user, dispute),
        'appeal_bond': 2 * dispute.bond_amount,
        'jurors': (dispute.jurors.select_related('juror').order_by('round', 'id')
                   if (closed_round or request.user.is_staff) else None),
        'juror_count': round_seats.count(),
        'votes_cast': round_seats.filter(taker_pct__isnull=False).count(),
        'vote_choices': JurorAssignment.VOTE_CHOICES,
        'timeline': dispute.task.events.select_related('actor'),
        'pct_choices': [(100, 'Taker gets everything'), (75, 'Taker 75% / creator 25%'), (50, 'Split 50/50'),
                        (25, 'Taker 25% / creator 75%'), (0, 'Creator gets a full refund')],
        'my_prestige': prestige.of(request.user),
    }
    return render(request, 'dispute_detail.html', context)


def _act(request, public_id, fn, success):
    dispute = _get(public_id)
    try:
        fn(dispute)
    except (TransitionError, ValueError) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, success)
    return redirect('dispute_detail', public_id=public_id)


@login_required(login_url='/login/')
@require_POST
@ratelimit('dispute_offer', '30/hour')
def make_offer(request, public_id):
    return _act(request, public_id, lambda d: disputes.make_offer(d, request.user, request.POST.get('taker_pct'),
                                                                  request.POST.get('message')), "Offer sent.")


@login_required(login_url='/login/')
@require_POST
def accept_offer(request, public_id):
    return _act(request, public_id, lambda d: disputes.accept_offer(d, request.user),
                "Settled. The reward was split as agreed and the bond refunded.")


@login_required(login_url='/login/')
@require_POST
def reject_offers(request, public_id):
    return _act(request, public_id, lambda d: disputes.reject_offers(d, request.user),
                "Sent to a jury.")


@login_required(login_url='/login/')
@require_POST
@ratelimit('dispute_evidence', '30/hour')
def add_evidence(request, public_id):
    return _act(request, public_id, lambda d: disputes.add_evidence(d, request.user, request.POST.get('text')),
                "Evidence added.")


@login_required(login_url='/login/')
@require_POST
def cast_vote(request, public_id):
    return _act(request, public_id, lambda d: disputes.cast_vote(d, request.user, request.POST.get('taker_pct'),
                                                                 request.POST.get('reason')),
                "Your vote was recorded. Votes stay hidden until the round closes.")


@login_required(login_url='/login/')
@require_POST
def appeal(request, public_id):
    return _act(request, public_id, lambda d: disputes.appeal(d, request.user),
                "Appeal filed. A new, larger jury will decide; its verdict is final.")


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, public_id):
    dispute = _get(public_id)
    try:
        disputes.withdraw(dispute, request.user)
    except TransitionError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"You have withdrawn the dispute for '{dispute.task.title}'. Your bond was refunded.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def moderator_resolve(request, public_id):
    return _act(request, public_id,
                lambda d: disputes.moderator_resolve(d, moderator=request.user, taker_pct=request.POST.get('taker_pct')),
                "Dispute resolved.")


__all__ = ['dispute_detail_view', 'make_offer', 'accept_offer', 'reject_offers', 'add_evidence', 'cast_vote',
           'appeal', 'withdraw_dispute', 'moderator_resolve', 'User']
