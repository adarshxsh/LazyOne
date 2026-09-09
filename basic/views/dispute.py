import os
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, DisputeEvidence

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    evidences = dispute.evidences.all().order_by('created_at')
    is_evidence_window_active = (dispute.status == 'open') and (dispute.expires_at is None or dispute.expires_at > timezone.now())

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'is_evidence_window_active': is_evidence_window_active
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
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')
        
        expires_at = timezone.now() + timedelta(days=3)
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason, expires_at=expires_at)
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "Evidence cannot be submitted for a closed or resolved dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.expires_at and timezone.now() > dispute.expires_at:
        messages.error(request, "The evidence submission window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    text = request.POST.get('text', '').strip()
    attachment_url = request.POST.get('attachment_url', '').strip()
    attachment_file = request.FILES.get('attachment')

    if not text:
        messages.error(request, "Evidence explanation text is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if attachment_url:
        if not (attachment_url.startswith('http://') or attachment_url.startswith('https://')):
            attachment_url = 'https://' + attachment_url
        val = URLValidator()
        try:
            val(attachment_url)
        except ValidationError:
            messages.error(request, "Invalid attachment URL format.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    if attachment_file:
        max_size = 5 * 1024 * 1024  # 5MB
        if attachment_file.size > max_size:
            messages.error(request, "Attached file exceeds maximum allowed size of 5MB.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        allowed_exts = ['.jpg', '.jpeg', '.png', '.pdf', '.doc', '.docx', '.txt', '.zip']
        ext = os.path.splitext(attachment_file.name)[1].lower()
        if ext not in allowed_exts:
            messages.error(request, f"Unsupported file format '{ext}'. Allowed formats: {', '.join(allowed_exts)}")
            return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        text=text,
        attachment_url=attachment_url if attachment_url else None,
        attachment=attachment_file if attachment_file else None
    )

    recipient = task.posted_by if request.user == task.taken_by else task.taken_by
    if recipient:
        Notification.objects.create(
            recipient=recipient,
            message=f"{request.user.username} submitted new counter-evidence for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)
