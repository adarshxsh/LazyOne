from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.core.exceptions import ValidationError
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger, validate_file_size, validate_file_extension
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_entries = dispute.evidence_entries.all()
    can_upload_evidence = (dispute.status == 'open') and (
        request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff
    )
    context = {
        'dispute': dispute,
        'task': task,
        'evidence_entries': evidence_entries,
        'can_upload_evidence': can_upload_evidence,
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

        uploaded_files = []
        for key in ['evidence_files', 'file', 'evidence_file']:
            uploaded_files.extend(request.FILES.getlist(key))

        seen = set()
        unique_files = []
        for f in uploaded_files:
            if id(f) not in seen:
                seen.add(id(f))
                unique_files.append(f)

        caption = request.POST.get('caption', '').strip()

        for f in unique_files:
            try:
                validate_file_size(f)
                validate_file_extension(f)
            except ValidationError as e:
                messages.error(request, f"Evidence file error ('{f.name}'): {e.message}")
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

            for f in unique_files:
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    uploaded_by=request.user,
                    file=f,
                    caption=caption
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
        messages.error(request, "Evidence can only be uploaded when the dispute is open.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    uploaded_files = []
    for key in ['evidence_files', 'file', 'evidence_file']:
        uploaded_files.extend(request.FILES.getlist(key))

    seen = set()
    unique_files = []
    for f in uploaded_files:
        if id(f) not in seen:
            seen.add(id(f))
            unique_files.append(f)

    if not unique_files:
        messages.error(request, "Please select at least one evidence file to upload.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    caption = request.POST.get('caption', '').strip()

    for f in unique_files:
        try:
            validate_file_size(f)
            validate_file_extension(f)
        except ValidationError as e:
            messages.error(request, f"Evidence file error ('{f.name}'): {e.message}")
            return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        for f in unique_files:
            DisputeEvidence.objects.create(
                dispute=dispute,
                uploaded_by=request.user,
                file=f,
                caption=caption
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
