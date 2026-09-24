from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.core.exceptions import ValidationError
from ..models import Dispute, Task, JurorAssignment, DisputeAuditEvent
from ..services.dispute import DisputeService


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = dispute.juror_assignments.filter(juror=request.user).exists()
    
    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    tier1_assignments = dispute.juror_assignments.filter(tier=1)
    tier2_assignments = dispute.juror_assignments.filter(tier=2)

    user_active_assignment = dispute.juror_assignments.filter(
        juror=request.user,
        tier=dispute.tier,
        has_voted=False
    ).first()

    can_vote = bool(user_active_assignment) and dispute.status in ['voting', 'appealed', 'under_appeal']
    can_appeal = is_participant and dispute.is_appealable()
    appeal_bond_amount = max(100, dispute.deposit_amount * 2) if can_appeal else 0

    audit_events = dispute.audit_events.all()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'tier1_assignments': tier1_assignments,
        'tier2_assignments': tier2_assignments,
        'can_vote': can_vote,
        'can_appeal': can_appeal,
        'appeal_bond_amount': appeal_bond_amount,
        'audit_events': audit_events,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'voting', 'appealed', 'under_appeal']:
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        try:
            dispute = DisputeService.raise_dispute(task, request.user, reason)
            messages.success(request, f"Dispute raised successfully. Deposit bond held and jury assigned.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        except ValidationError as e:
            messages.error(request, str(e.message if hasattr(e, 'message') else e))
            return redirect('my_tasks')

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    try:
        DisputeService.withdraw_dispute(dispute, request.user)
        messages.success(request, f"You have successfully withdrawn the dispute for '{dispute.task.title}'. Your deposit bond has been refunded.")
    except ValidationError as e:
        messages.error(request, str(e.message if hasattr(e, 'message') else e))

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def file_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    appeal_reason = request.POST.get('appeal_reason', '').strip()

    if not appeal_reason:
        messages.error(request, "Please provide a reason for filing the appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        DisputeService.file_appeal(dispute, request.user, appeal_reason)
        messages.success(request, f"Appeal filed successfully. Tier 2 Grand Jury assigned for review.")
    except ValidationError as e:
        messages.error(request, str(e.message if hasattr(e, 'message') else e))

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    vote_choice = request.POST.get('vote', '').strip()

    try:
        DisputeService.cast_juror_vote(dispute, request.user, vote_choice)
        messages.success(request, "Your vote has been submitted successfully.")
    except ValidationError as e:
        messages.error(request, str(e.message if hasattr(e, 'message') else e))

    return redirect('dispute_detail', dispute_id=dispute.id)
