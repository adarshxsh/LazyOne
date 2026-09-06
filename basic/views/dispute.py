from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.http import JsonResponse
from django.db import transaction
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger

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
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    open_dispute = task.disputes.filter(status='open').first()
    if open_dispute:
        return redirect('dispute_detail', dispute_id=open_dispute.id)

    if request.user not in (task.taken_by, task.posted_by) or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        uploaded_files = (
            request.FILES.getlist('evidence_files') or
            request.FILES.getlist('attachments') or
            request.FILES.getlist('files') or
            request.FILES.getlist('file')
        )

        for f in uploaded_files:
            if f.size > 10 * 1024 * 1024:
                messages.error(request, "File attachments must not exceed 10 MB per upload.")
                return redirect('my_tasks')

        expires_at = timezone.now() + timedelta(hours=72)
        with transaction.atomic():
            dispute = Dispute.objects.create(
                task=task,
                raised_by=request.user,
                reason=reason,
                expires_at=expires_at
            )
            task.status = 'disputed'
            task.save()

            for f in uploaded_files:
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    sender=request.user,
                    file=f
                )

            recipient = task.posted_by if request.user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, "Dispute raised successfully.")
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
        messages.error(request, "Cannot submit evidence for a closed dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    content = request.POST.get('content', '').strip()
    uploaded_files = (
        request.FILES.getlist('evidence_files') or
        request.FILES.getlist('attachments') or
        request.FILES.getlist('files') or
        request.FILES.getlist('file')
    )

    if not content and not uploaded_files:
        messages.error(request, "Please provide text content or attach a file as evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    for f in uploaded_files:
        if f.size > 10 * 1024 * 1024:
            messages.error(request, "File attachments must not exceed 10 MB per upload.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if uploaded_files:
            first = True
            for f in uploaded_files:
                DisputeEvidence.objects.create(
                    dispute=dispute,
                    sender=request.user,
                    content=content if first else '',
                    file=f
                )
                first = False
        else:
            DisputeEvidence.objects.create(
                dispute=dispute,
                sender=request.user,
                content=content
            )

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} submitted counter-evidence for dispute on '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open.")
        return redirect('my_tasks')

    task = dispute.task
    with transaction.atomic():
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

def auto_resolve_disputes(request):
    """
    Scheduled webhook background endpoint to auto-resolve expired disputes.
    """
    expired_disputes = Dispute.objects.filter(
        status='open',
        expires_at__isnull=False,
        expires_at__lte=timezone.now()
    )

    resolved_records = []

    for dispute in expired_disputes:
        with transaction.atomic():
            task = dispute.task
            raised_by = dispute.raised_by
            counterparty = task.posted_by if raised_by == task.taken_by else task.taken_by

            evidences = dispute.evidences.order_by('-created_at')
            last_evidence = evidences.first()

            if not last_evidence:
                winner = raised_by
            else:
                winner = last_evidence.sender

            if not winner or winner not in (task.posted_by, task.taken_by):
                winner = raised_by if raised_by else task.taken_by

            loser = task.posted_by if winner == task.taken_by else task.taken_by

            if winner == task.taken_by:
                if task.taken_by:
                    winner_profile = task.taken_by.userprofile
                    winner_profile.rewards += task.reward
                    winner_profile.save()

                task.status = 'completed'
                task.save()
                dispute.status = 'auto_resolved'
                dispute.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_settlement',
                    description=f"Automated dispute settlement points awarded for task: '{task.title}'"
                )
            else:
                if task.posted_by:
                    winner_profile = task.posted_by.userprofile
                    winner_profile.rewards += task.reward
                    winner_profile.save()

                task.status = 'cancelled'
                task.save()
                dispute.status = 'auto_resolved'
                dispute.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_settlement',
                    description=f"Automated dispute settlement points refunded for task: '{task.title}'"
                )

            if winner:
                Notification.objects.create(
                    recipient=winner,
                    message=f"Dispute for '{task.title}' was automatically resolved in your favor due to deadline expiration.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            if loser:
                Notification.objects.create(
                    recipient=loser,
                    message=f"Dispute for '{task.title}' was automatically resolved due to response deadline expiration.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            resolved_records.append({
                'dispute_id': dispute.id,
                'task_id': task.id,
                'winner': winner.username if winner else None,
                'status': dispute.status
            })

    return JsonResponse({
        'status': 'success',
        'processed_count': len(resolved_records),
        'resolved_disputes': resolved_records
    })
