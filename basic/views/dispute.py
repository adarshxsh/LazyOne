from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.views.decorators.http import require_POST
from ..models import Dispute, Task
from ..services.dispute import DisputeService

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    # Check authorization: task litigants, staff, or assigned jurors
    is_litigant = request.user in [task.posted_by, task.taken_by, dispute.raised_by]
    is_juror = dispute.jurors.filter(id=request.user.id).exists()
    is_staff = request.user.is_staff or request.user.is_superuser

    if not (is_litigant or is_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    audit_events = dispute.audit_events.all().select_related('actor')

    context = {
        'dispute': dispute,
        'task': task,
        'audit_events': audit_events,
        'is_litigant': is_litigant,
        'is_juror': is_juror,
        'is_staff': is_staff,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.method == 'POST':
        reason = request.POST.get('reason')
        try:
            dispute = DisputeService.raise_dispute(task, request.user, reason)
            messages.success(request, f"Dispute raised successfully. {dispute.deposit_amount} points held as deposit bond.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        except (ValidationError, PermissionDenied) as e:
            msg = e.message if hasattr(e, 'message') else str(e)
            if isinstance(e.args[0], list):
                msg = e.args[0][0]
            messages.error(request, msg)
            return redirect('my_tasks')

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    try:
        DisputeService.withdraw_dispute(dispute, request.user)
        messages.success(request, f"You have successfully withdrawn the dispute for '{dispute.task.title}'. Your deposit bond has been refunded.")
    except (ValidationError, PermissionDenied) as e:
        msg = e.message if hasattr(e, 'message') else str(e)
        if isinstance(e.args[0], list):
            msg = e.args[0][0]
        messages.error(request, msg)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    outcome = request.POST.get('outcome')
    try:
        DisputeService.resolve_dispute(dispute, request.user, outcome)
        messages.success(request, f"Dispute for '{dispute.task.title}' has been successfully resolved.")
    except (ValidationError, PermissionDenied) as e:
        msg = e.message if hasattr(e, 'message') else str(e)
        if isinstance(e.args[0], list):
            msg = e.args[0][0]
        messages.error(request, msg)

    return redirect('dispute_detail', dispute_id=dispute.id)
