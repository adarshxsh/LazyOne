from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Dispute, JurorAssignment
from ..services import disputes
from ..services.marketplace import TransitionError


def _get(public_id):
    return get_object_or_404(Dispute.objects.select_related('task__posted_by', 'task__taken_by', 'raised_by'),
                             public_id=public_id)


@login_required(login_url='/login/')
def dispute_detail_view(request, public_id):
    dispute = _get(public_id)
    if not disputes.can_view(request.user, dispute):
        messages.error(request, "Only the people involved, the jury and moderators can view this dispute.")
        return redirect('home')
    seat = dispute.jurors.filter(juror=request.user).first()
    closed = not dispute.is_active
    context = {
        'dispute': dispute,
        'task': dispute.task,
        'evidence': dispute.evidence.select_related('submitted_by'),
        'is_party': disputes.is_party(request.user, dispute),
        'seat': seat,
        'can_withdraw': disputes.can_withdraw(request.user, dispute),
        'jurors': dispute.jurors.select_related('juror') if (closed or request.user.is_staff) else None,
        'juror_count': dispute.jurors.count(),
        'votes_cast': dispute.jurors.exclude(vote='').count(),
        'vote_choices': JurorAssignment.Vote.choices,
    }
    return render(request, 'dispute_detail.html', context)


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
def add_evidence(request, public_id):
    dispute = _get(public_id)
    try:
        disputes.add_evidence(dispute, request.user, request.POST.get('text'))
    except TransitionError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Evidence added.")
    return redirect('dispute_detail', public_id=public_id)


@login_required(login_url='/login/')
@require_POST
def cast_vote(request, public_id):
    dispute = _get(public_id)
    try:
        disputes.cast_vote(dispute, request.user, request.POST.get('vote'))
    except TransitionError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Your vote was recorded. Votes stay hidden until the dispute closes.")
    return redirect('dispute_detail', public_id=public_id)


@login_required(login_url='/login/')
@require_POST
def moderator_resolve(request, public_id):
    dispute = _get(public_id)
    winner = get_object_or_404(User, pk=request.POST.get('winner_id'))
    try:
        disputes.moderator_resolve(dispute, winner, moderator=request.user)
    except (TransitionError, ValueError) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Dispute resolved.")
    return redirect('dispute_detail', public_id=public_id)
