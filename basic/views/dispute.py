from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence, JurorAssignment
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = dispute.is_juror(request.user)
    is_staff_user = request.user.is_staff or request.user.is_superuser

    if not is_participant and not is_juror and not is_staff_user:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    task_conversation = task.conversation
    deliberation_conversation = None
    if is_juror or is_staff_user:
        deliberation_conversation = dispute.deliberation_conversation

    evidence_list = dispute.evidence_entries.all()

    context = {
        'dispute': dispute,
        'task': task,
        'task_conversation': task_conversation,
        'deliberation_conversation': deliberation_conversation,
        'evidence_list': evidence_list,
        'is_participant': is_participant,
        'is_juror': is_juror,
        'is_staff': is_staff_user,
        'can_submit_evidence': is_participant and dispute.status == 'open',
        'can_view_deliberation': (is_juror or is_staff_user) and not is_participant,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "Evidence can only be submitted for open disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '').strip() or request.POST.get('text_statement', '').strip()
    evidence_url = request.POST.get('evidence_url', '').strip() or request.POST.get('proof_url', '').strip()
    file_attachment = request.FILES.get('file')

    if not description and not evidence_url and not file_attachment:
        messages.error(request, "Please provide a description, proof URL, or file attachment.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        description=description,
        evidence_url=evidence_url or None,
        file=file_attachment or None
    )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

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
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        user_profile = request.user.userprofile
        if user_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
