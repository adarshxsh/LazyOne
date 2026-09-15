import os
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, DisputeEvidence, Task, Notification
from django.views.decorators.http import require_POST
from django.urls import reverse

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.pdf', '.txt', '.zip'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

def validate_evidence_file(file_obj):
    ext = os.path.splitext(file_obj.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"Invalid file type '{ext}'. Allowed extensions: .png, .jpg, .jpeg, .pdf, .txt, .zip"
    if file_obj.size > MAX_FILE_SIZE:
        return False, "File size exceeds the 10MB limit."
    return True, ""

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_list = dispute.evidence_entries.all().order_by('created_at')
    can_upload = (dispute.status == 'open') and (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff)

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_list': evidence_list,
        'can_upload': can_upload,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to raise a dispute for this task.")
        return redirect('my_tasks')
    if task.status != 'in_progress':
        messages.error(request, "Disputes can only be raised for tasks currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        file_obj = request.FILES.get('file') or request.FILES.get('evidence') or request.FILES.get('evidence_file')
        if file_obj:
            is_valid, error_msg = validate_evidence_file(file_obj)
            if not is_valid:
                messages.error(request, error_msg)
                return redirect('my_tasks')

        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
        task.status = 'disputed'
        task.save()

        if file_obj:
            description = request.POST.get('description') or request.POST.get('evidence_description') or ''
            DisputeEvidence.objects.create(
                dispute=dispute,
                uploaded_by=request.user,
                file=file_obj,
                description=description
            )

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def upload_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to upload evidence for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "Cannot upload evidence to a resolved or closed dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    file_obj = request.FILES.get('file') or request.FILES.get('evidence') or request.FILES.get('evidence_file')
    if not file_obj:
        messages.error(request, "Please select a file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    is_valid, error_msg = validate_evidence_file(file_obj)
    if not is_valid:
        messages.error(request, error_msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description') or request.POST.get('evidence_description') or ''

    DisputeEvidence.objects.create(
        dispute=dispute,
        uploaded_by=request.user,
        file=file_obj,
        description=description
    )

    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
    if counterparty and counterparty != request.user:
        Notification.objects.create(
            recipient=counterparty,
            message=f"{request.user.username} uploaded evidence for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence uploaded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

