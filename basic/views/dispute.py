import os
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from ..models import Dispute, Task, Notification, DisputeEvidence
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_items = dispute.evidence_items.all().order_by('created_at')
    file_attachments_count = dispute.evidence_items.filter(file__isnull=False).exclude(file='').count()
    is_expired = dispute.expires_at and timezone.now() >= dispute.expires_at
    can_upload_evidence = (
        dispute.status == 'open' and
        not is_expired and
        (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff)
    )

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_items': evidence_items,
        'file_attachments_count': file_attachments_count,
        'is_expired': is_expired,
        'can_upload_evidence': can_upload_evidence,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def upload_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to upload evidence for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "Cannot upload evidence to a resolved or closed dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.expires_at and timezone.now() >= dispute.expires_at:
        messages.error(request, "Cannot upload evidence for an expired dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '').strip()
    evidence_file = request.FILES.get('file')

    if not description and not evidence_file:
        messages.error(request, "Please provide a description or attach a file as evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if evidence_file:
        existing_file_count = dispute.evidence_items.filter(file__isnull=False).exclude(file='').count()
        if existing_file_count >= 5:
            messages.error(request, "Maximum limit of 5 evidence file attachments reached for this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if evidence_file.size > 5 * 1024 * 1024:
            messages.error(request, "File size exceeds the 5 MB limit.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        ext = os.path.splitext(evidence_file.name)[1].lower()
        allowed_extensions = ['.jpg', '.jpeg', '.png', '.pdf']
        if ext not in allowed_extensions:
            messages.error(request, "Invalid file format. Only JPEG, PNG, and PDF files are allowed.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        uploaded_by=request.user,
        file=evidence_file if evidence_file else None,
        description=description
    )

    recipient = task.posted_by if request.user == task.taken_by else task.taken_by
    if recipient and recipient != request.user:
        Notification.objects.create(
            recipient=recipient,
            message=f"{request.user.username} uploaded evidence for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


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
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
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
