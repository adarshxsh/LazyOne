from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from ..models import Dispute, Task
from ..services.dispute import DisputeService

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    audit_events = dispute.audit_events.all().order_by('created_at')
    context = {
        'dispute': dispute,
        'task': task,
        'audit_events': audit_events,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        try:
            dispute = DisputeService.raise_dispute(task=task, actor=request.user, reason=reason)
            deposit_amount = task.deposit_bond_amount
            messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('my_tasks')
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    DisputeService.withdraw_dispute(dispute=dispute, actor=request.user)
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
