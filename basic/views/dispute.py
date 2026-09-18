import os
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.http import FileResponse
from ..models import Dispute, DisputeAttachment, Task, Notification, RewardLedger

ALLOWED_ATTACHMENT_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.txt', '.zip', '.log'}
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB

def validate_dispute_attachment(uploaded_file):
    if uploaded_file.size > MAX_ATTACHMENT_SIZE:
        return f"File '{uploaded_file.name}' exceeds the maximum allowed size of 10MB."
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return f"File type '{ext}' is not allowed. Allowed types: .png, .jpg, .jpeg, .gif, .pdf, .txt, .zip, .log"
    return None

def get_uploaded_files(request):
    files = request.FILES.getlist('attachments') or request.FILES.getlist('attachment') or request.FILES.getlist('files') or request.FILES.getlist('file')
    if not files and request.FILES:
        files = list(request.FILES.values())
    return files

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    context = {
        'dispute': dispute,
        'task': task
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

        deposit_amount = task.deposit_bond_amount
        user_profile = request.user.userprofile
        if user_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        files = get_uploaded_files(request)
        for f in files:
            err = validate_dispute_attachment(f)
            if err:
                messages.error(request, err)
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

            for f in files:
                DisputeAttachment.objects.create(
                    dispute=dispute,
                    uploaded_by=request.user,
                    file=f,
                    file_name=os.path.basename(f.name),
                    file_size=f.size
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
def upload_dispute_attachment(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to upload attachments to this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "Cannot upload attachments to a closed or resolved dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    files = get_uploaded_files(request)
    if not files:
        messages.error(request, "Please select at least one file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    for f in files:
        err = validate_dispute_attachment(f)
        if err:
            messages.error(request, err)
            return redirect('dispute_detail', dispute_id=dispute.id)

    count = 0
    for f in files:
        DisputeAttachment.objects.create(
            dispute=dispute,
            uploaded_by=request.user,
            file=f,
            file_name=os.path.basename(f.name),
            file_size=f.size
        )
        count += 1

    messages.success(request, f"Successfully uploaded {count} attachment(s).")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def download_dispute_attachment(request, attachment_id):
    attachment = get_object_or_404(DisputeAttachment, id=attachment_id)
    task = attachment.dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view or download this attachment.")
        return redirect('home')

    is_inline = request.GET.get('inline') == '1'
    try:
        response = FileResponse(
            attachment.file.open('rb'),
            as_attachment=not is_inline,
            filename=attachment.file_name
        )
        return response
    except FileNotFoundError:
        messages.error(request, "The requested file could not be found.")
        return redirect('dispute_detail', dispute_id=attachment.dispute.id)

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
