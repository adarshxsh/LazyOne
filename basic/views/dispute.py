import os
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    evidences = dispute.evidences.all()
    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'is_evidence_open': dispute.is_evidence_window_open,
        'now': timezone.now(),
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

        now = timezone.now()
        evidence_deadline = now + timedelta(days=3)
        expires_at = now + timedelta(days=7)

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
                dispute.evidence_deadline = evidence_deadline
                dispute.expires_at = expires_at
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    evidence_deadline=evidence_deadline,
                    expires_at=expires_at
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
def submit_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_evidence_window_open:
        messages.error(request, "The evidence submission window for this dispute has closed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '').strip()
    file = request.FILES.get('file')

    if not description and not file:
        messages.error(request, "Please provide a description or attach a file.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if file:
        if file.size > 5 * 1024 * 1024:
            messages.error(request, "File size cannot exceed 5MB.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        ext = os.path.splitext(file.name)[1].lower().lstrip('.')
        allowed_exts = ['png', 'jpg', 'jpeg', 'pdf', 'txt']
        if ext not in allowed_exts:
            messages.error(request, f"File format '.{ext}' is not allowed. Allowed formats: PNG, JPG, JPEG, PDF, TXT.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=request.user,
            description=description,
            file=file
        )

        other_party = None
        if request.user == task.posted_by:
            other_party = task.taken_by
        elif request.user == task.taken_by:
            other_party = task.posted_by

        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"{request.user.username} submitted new evidence for dispute on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Evidence submitted successfully.")
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
