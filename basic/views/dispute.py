import os
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

ALLOWED_EVIDENCE_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.txt'}
MAX_EVIDENCE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB in bytes

def validate_evidence_file(file):
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_EVIDENCE_EXTENSIONS:
        return f"Invalid file type '{ext}' for file '{file.name}'. Allowed file types are: PDF, PNG, JPG, JPEG, TXT."
    if file.size > MAX_EVIDENCE_FILE_SIZE:
        return f"File '{file.name}' exceeds the maximum allowed file size of 10 MB."
    return None

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    evidence_list = dispute.evidence.all()
    can_upload = dispute.status == 'open' and (
        request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff
    )
    
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

        evidence_files = request.FILES.getlist('evidence_files') or request.FILES.getlist('evidence') or request.FILES.getlist('files')
        description = request.POST.get('description', '') or request.POST.get('evidence_description', '')

        for file in evidence_files:
            error_msg = validate_evidence_file(file)
            if error_msg:
                messages.error(request, error_msg)
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

            for file in evidence_files:
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    uploaded_by=request.user,
                    file=file,
                    description=description
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
def upload_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to upload evidence for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "Evidence can only be uploaded during an active dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    evidence_files = request.FILES.getlist('evidence_files') or request.FILES.getlist('evidence') or request.FILES.getlist('files')
    description = request.POST.get('description', '') or request.POST.get('evidence_description', '')

    if not evidence_files:
        messages.error(request, "Please select at least one evidence file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    for file in evidence_files:
        error_msg = validate_evidence_file(file)
        if error_msg:
            messages.error(request, error_msg)
            return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        for file in evidence_files:
            DisputeEvidence.objects.create(
                dispute=dispute,
                uploaded_by=request.user,
                file=file,
                description=description
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
