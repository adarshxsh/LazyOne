from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeEvidence, Task, Notification, validate_evidence_file

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    if request.method == 'POST':
        if dispute.status != 'open':
            messages.error(request, "This dispute is already closed.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if dispute.response_deadline and timezone.now() > dispute.response_deadline:
            messages.error(request, "The deadline for submitting evidence on this dispute has expired.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        evidence_text = request.POST.get('text') or request.POST.get('reason')
        if not evidence_text or not evidence_text.strip():
            messages.error(request, "Evidence text is required.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        attachment = request.FILES.get('attachment')
        if attachment:
            try:
                validate_evidence_file(attachment)
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
                return redirect('dispute_detail', dispute_id=dispute.id)

        # Determine evidence type and stage transition
        is_respondent = (request.user != dispute.raised_by)
        if is_respondent and dispute.dispute_stage == 'counter_evidence':
            evidence_type = 'counter_evidence'
            dispute.dispute_stage = 'under_review'
            dispute.save()
        else:
            evidence_type = 'supplemental'

        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=request.user,
            text=evidence_text.strip(),
            file=attachment,
            evidence_type=evidence_type
        )

        recipient = task.taken_by if request.user == task.posted_by else task.posted_by
        type_display = 'counter-evidence' if evidence_type == 'counter_evidence' else 'supplemental evidence'
        Notification.objects.create(
            recipient=recipient,
            message=f"{request.user.username} submitted {type_display} for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        messages.success(request, f"Your {type_display} has been submitted successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    evidences = dispute.evidences.all().order_by('created_at')
    now = timezone.now()
    is_expired = dispute.response_deadline and now > dispute.response_deadline
    can_submit = (dispute.status == 'open') and (not is_expired) and (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff)

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'is_expired': is_expired,
        'can_submit': can_submit,
        'now': now,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason or not reason.strip():
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        attachment = request.FILES.get('attachment')
        if attachment:
            try:
                validate_evidence_file(attachment)
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
                return redirect('my_tasks')

        deadline = timezone.now() + timedelta(hours=72)
        dispute = Dispute.objects.create(
            task=task,
            raised_by=request.user,
            reason=reason.strip(),
            response_deadline=deadline,
            dispute_stage='counter_evidence'
        )

        # Create initial evidence entry
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=request.user,
            text=reason.strip(),
            file=attachment,
            evidence_type='initial_proof'
        )

        task.status = 'disputed'
        task.save()

        # Notify task poster (respondent)
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. Response deadline: {deadline.strftime('%Y-%m-%d %H:%M UTC')}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

        messages.success(request, "Dispute raised successfully. Counter-evidence deadline set to 72 hours.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.raised_by != request.user:
        messages.error(request, "You are not authorized to withdraw this dispute.")
        return redirect('home')

    task = dispute.task
    task.status = 'in_progress'
    task.save()

    dispute.status = 'resolved'
    dispute.dispute_stage = 'resolved'
    dispute.resolved_at = timezone.now()
    dispute.resolution_outcome = 'withdrawn'
    dispute.save()

    recipient = task.posted_by if request.user == task.taken_by else task.taken_by
    Notification.objects.create(
        recipient=recipient,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')
