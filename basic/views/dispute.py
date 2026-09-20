import os
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden, FileResponse
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

ALLOWED_EVIDENCE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.pdf', '.txt', '.zip'}
MAX_EVIDENCE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

def validate_evidence_file(uploaded_file):
    if uploaded_file.size > MAX_EVIDENCE_FILE_SIZE:
        return False, f"File '{uploaded_file.name}' exceeds the maximum allowed size of 10 MB."
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EVIDENCE_EXTENSIONS:
        return False, f"File '{uploaded_file.name}' has an unsupported extension. Allowed extensions: .png, .jpg, .jpeg, .pdf, .txt, .zip"
    return True, ""

def extract_uploaded_files(request):
    files = []
    for key in ['evidence', 'file', 'files']:
        if key in request.FILES:
            files.extend(request.FILES.getlist(key))
    if not files and request.FILES:
        for key in request.FILES:
            files.extend(request.FILES.getlist(key))
    return files

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    evidence_list = dispute.evidence.all()
    context = {
        'dispute': dispute,
        'task': task,
        'evidence_list': evidence_list,
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

        files = extract_uploaded_files(request)
        for uploaded_file in files:
            is_valid, err_msg = validate_evidence_file(uploaded_file)
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

            description = request.POST.get('description', '')
            for uploaded_file in files:
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    uploaded_by=request.user,
                    file=uploaded_file,
                    file_size=uploaded_file.size,
                    original_filename=uploaded_file.name,
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
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "Evidence can only be uploaded while the dispute is open.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    files = extract_uploaded_files(request)
    if not files:
        messages.error(request, "No evidence file was uploaded.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    for uploaded_file in files:
        is_valid, err_msg = validate_evidence_file(uploaded_file)
        if not is_valid:
            messages.error(request, err_msg)
            return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '')
    with transaction.atomic():
        for uploaded_file in files:
            DisputeEvidence.objects.create(
                dispute=dispute,
                uploaded_by=request.user,
                file=uploaded_file,
                file_size=uploaded_file.size,
                original_filename=uploaded_file.name,
                description=description
            )
    messages.success(request, f"Successfully uploaded {len(files)} evidence file(s).")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def download_dispute_evidence(request, evidence_id):
    evidence = get_object_or_404(DisputeEvidence, id=evidence_id)
    dispute = evidence.dispute
    task = dispute.task

    is_counterparty = (request.user == task.posted_by or request.user == task.taken_by)
    is_staff = request.user.is_staff or request.user.is_superuser
    is_juror = False
    if hasattr(dispute, 'jurors') and dispute.jurors.filter(id=request.user.id).exists():
        is_juror = True
    elif hasattr(dispute, 'votes') and dispute.votes.filter(voter=request.user).exists():
        is_juror = True

    if not (is_counterparty or is_staff or is_juror):
        return HttpResponseForbidden("You are not authorized to download this evidence.")

    try:
        file_handle = evidence.file.open('rb')
    except (FileNotFoundError, ValueError):
        messages.error(request, "Requested evidence file could not be found.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    response = FileResponse(file_handle, as_attachment=True, filename=evidence.original_filename)
    return response

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

