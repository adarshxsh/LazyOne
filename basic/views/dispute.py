import os
import mimetypes
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB limit

ALLOWED_MIME_PREFIXES = ('image/', 'text/')
ALLOWED_EXACT_MIMES = (
    'application/pdf', 'application/json', 'application/zip',
    'application/x-zip-compressed', 'application/x-zip',
    'application/x-7z-compressed', 'application/x-tar', 'application/gzip'
)
ALLOWED_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg',
    '.pdf', '.txt', '.csv', '.log', '.json',
    '.zip', '.tar', '.gz', '.7z'
)

def validate_uploaded_file(file):
    if file.size > MAX_FILE_SIZE:
        return False, f"File '{file.name}' exceeds maximum allowed size of 10MB."

    content_type = getattr(file, 'content_type', '') or ''
    ext = os.path.splitext(file.name)[1].lower()

    is_mime_valid = (
        content_type.startswith(ALLOWED_MIME_PREFIXES) or
        content_type in ALLOWED_EXACT_MIMES
    )
    is_ext_valid = ext in ALLOWED_EXTENSIONS

    if not (is_mime_valid or is_ext_valid):
        return False, f"File type for '{file.name}' is not allowed. Allowed types: images, PDFs, text logs, zip archives."

    return True, ""


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        return HttpResponseForbidden("You are not authorized to view this dispute.")

    evidence_list = dispute.evidence.all().order_by('uploaded_at')
    context = {
        'dispute': dispute,
        'task': task,
        'evidence_list': evidence_list,
        'is_counterparty': request.user == task.posted_by or request.user == task.taken_by,
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
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        files = request.FILES.getlist('evidence_files') or request.FILES.getlist('evidence') or list(request.FILES.values())

        for f in files:
            is_valid, err_msg = validate_uploaded_file(f)
            if not is_valid:
                messages.error(request, err_msg)
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

            title = request.POST.get('title', '')
            description = request.POST.get('description', '')
            evidence_type = request.POST.get('evidence_type', 'deliverable_proof')

            for f in files:
                mime_type = getattr(f, 'content_type', '') or mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    submitted_by=request.user,
                    file=f,
                    title=title or f.name,
                    description=description,
                    evidence_type=evidence_type,
                    file_size=f.size,
                    mime_type=mime_type,
                )

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
def add_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        return HttpResponseForbidden("You are not authorized to upload evidence for this dispute.")

    files = request.FILES.getlist('evidence_files') or request.FILES.getlist('evidence') or list(request.FILES.values())
    if not files:
        messages.error(request, "Please select at least one file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    for f in files:
        is_valid, err_msg = validate_uploaded_file(f)
        if not is_valid:
            messages.error(request, err_msg)
            return redirect('dispute_detail', dispute_id=dispute.id)

    title = request.POST.get('title', '')
    description = request.POST.get('description', '')
    evidence_type = request.POST.get('evidence_type', 'other')

    for f in files:
        mime_type = getattr(f, 'content_type', '') or mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=request.user,
            file=f,
            title=title or f.name,
            description=description,
            evidence_type=evidence_type,
            file_size=f.size,
            mime_type=mime_type,
        )

    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"{request.user.username} uploaded new evidence for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence uploaded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


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
