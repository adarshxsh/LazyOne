from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db.models import Q
from django.core.exceptions import ValidationError

from ..models import Dispute, Task, Notification
from ..services import DisputeLifecycleService


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    is_staff = request.user.is_staff

    if not (is_participant or is_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_entries = dispute.evidence_entries.all().order_by('created_at')
    user_vote = dispute.votes.filter(voter=request.user).first()

    can_submit_evidence = is_participant and dispute.status in [Dispute.EVIDENCE_COLLECTION, Dispute.OPEN]
    can_vote = is_juror and dispute.status == Dispute.VOTING and user_vote is None
    can_appeal = is_participant and dispute.status in [Dispute.VOTING, Dispute.APPEAL] and dispute.status not in [Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED]
    can_withdraw = request.user == dispute.raised_by and dispute.status in [Dispute.OPEN, Dispute.EVIDENCE_COLLECTION, Dispute.JURY_SELECTION]

    total_votes = dispute.votes.count()
    poster_votes = dispute.votes.filter(Q(choice='poster') | Q(voted_for=task.posted_by)).count()
    taker_votes = dispute.votes.filter(Q(choice='taker') | Q(voted_for=task.taken_by)).count()

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_entries': evidence_entries,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'is_staff': is_staff,
        'user_vote': user_vote,
        'can_submit_evidence': can_submit_evidence,
        'can_vote': can_vote,
        'can_appeal': can_appeal,
        'can_withdraw': can_withdraw,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'assigned_jurors_count': dispute.jury_assignments.count(),
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in [Dispute.EVIDENCE_COLLECTION, Dispute.JURY_SELECTION, Dispute.VOTING, Dispute.APPEAL]:
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        try:
            dispute = DisputeLifecycleService.raise_dispute(task, request.user, reason)
            messages.success(request, f"Dispute raised successfully. {dispute.deposit_amount} points held as deposit bond. You are now in the evidence collection phase.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        except ValidationError as e:
            messages.error(request, str(e.message) if hasattr(e, 'message') else str(e))
            return redirect('my_tasks')

    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_evidence_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    text_evidence = request.POST.get('text_evidence', '')
    external_link = request.POST.get('external_link', None) or None
    file_attachment = request.FILES.get('file_attachment', None)

    try:
        DisputeLifecycleService.submit_evidence(
            dispute=dispute,
            user=request.user,
            text_evidence=text_evidence,
            external_link=external_link,
            file_attachment=file_attachment
        )
        messages.success(request, "Evidence submitted successfully.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def cast_vote_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    choice = request.POST.get('choice')

    if not choice:
        messages.error(request, "Please select an outcome to vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        DisputeLifecycleService.cast_vote(dispute, request.user, choice)
        messages.success(request, "Your vote has been cast successfully.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def request_appeal_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    reason = request.POST.get('reason', '')

    try:
        DisputeLifecycleService.request_appeal(dispute, request.user, reason)
        messages.success(request, "Appeal requested. An admin will review the dispute.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff)
@require_POST
def resolve_dispute_admin_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    winner = request.POST.get('winner')

    try:
        DisputeLifecycleService.resolve_dispute(dispute, winner)
        messages.success(request, f"Dispute resolved in favor of {winner}.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    try:
        DisputeLifecycleService.withdraw_dispute(dispute, request.user)
        messages.success(request, f"You have successfully withdrawn the dispute for '{dispute.task.title}'. Your deposit bond has been refunded.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('my_tasks')
