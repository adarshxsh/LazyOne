from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger, validate_evidence_file
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import timedelta

def process_dispute_expiration(dispute):
    """
    Checks if an open dispute has reached or passed its evidence deadline.
    If so, resolves the dispute automatically in favor of the active party or dispute initiator,
    transfers/refunds reward points with RewardLedger entries, and notifies both parties.
    Returns True if dispute was expired and resolved by this call, False otherwise.
    """
    if dispute.status != 'open':
        return False
    
    if timezone.now() <= dispute.evidence_deadline:
        return False

    with transaction.atomic():
        # Re-fetch dispute with lock to avoid race conditions
        dispute = Dispute.objects.select_for_update().get(id=dispute.id)
        if dispute.status != 'open':
            return False

        task = dispute.task
        posted_by_has_ev = dispute.evidences.filter(user=task.posted_by).exists()
        taken_by_has_ev = dispute.evidences.filter(user=task.taken_by).exists() if task.taken_by else False

        if taken_by_has_ev and not posted_by_has_ev:
            favored_user = task.taken_by
            reason = "Auto-resolved in favor of task taker due to poster evidence submission timeout."
        elif posted_by_has_ev and not taken_by_has_ev:
            favored_user = task.posted_by
            reason = "Auto-resolved in favor of task poster due to taker evidence submission timeout."
        else:
            favored_user = dispute.raised_by
            reason = f"Auto-resolved in favor of {dispute.raised_by.username} due to dispute evidence deadline timeout."

        if favored_user == task.taken_by and task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Dispute auto-resolved payout for task: '{task.title}'"
            )
            task.status = 'completed'
        else:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Dispute auto-resolved refund for task: '{task.title}'"
            )
            task.status = 'cancelled'

        if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
            if favored_user == dispute.raised_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded for auto-resolved dispute on task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=favored_user,
                    reason_description=f"Security deposit bond forfeited for auto-resolved dispute on task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.is_expired = True
        dispute.resolution_reason = reason
        dispute.save()
        task.save()

        # Send notifications
        notification_text = f"Dispute for task '{task.title}' expired and was auto-resolved: {reason}"
        detail_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(recipient=task.posted_by, message=notification_text, link=detail_link)
        if task.taken_by:
            Notification.objects.create(recipient=task.taken_by, message=notification_text, link=detail_link)

    return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    process_dispute_expiration(dispute)
    dispute.refresh_from_db()

    evidences = dispute.evidences.all().order_by('created_at')
    now = timezone.now()
    if dispute.evidence_deadline > now:
        time_remaining = dispute.evidence_deadline - now
        remaining_days = time_remaining.days
        remaining_hours = time_remaining.seconds // 3600
    else:
        time_remaining = None
        remaining_days = 0
        remaining_hours = 0

    can_submit_evidence = (
        dispute.status == 'open' and
        not dispute.is_expired and
        now <= dispute.evidence_deadline and
        (request.user == task.posted_by or request.user == task.taken_by)
    )

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'now': now,
        'time_remaining': time_remaining,
        'remaining_days': remaining_days,
        'remaining_hours': remaining_hours,
        'can_submit_evidence': can_submit_evidence,
        'evidence_type_choices': DisputeEvidence.EVIDENCE_TYPES,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    process_dispute_expiration(dispute)
    dispute.refresh_from_db()

    now = timezone.now()
    if dispute.status != 'open' or dispute.is_expired or now > dispute.evidence_deadline:
        messages.error(request, "Evidence submission is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    evidence_type = request.POST.get('evidence_type', 'text')
    file_attachment = request.FILES.get('file_attachment')

    if not title:
        messages.error(request, "Evidence title is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if file_attachment:
        try:
            validate_evidence_file(file_attachment)
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, 'message') else str(e))
            return redirect('dispute_detail', dispute_id=dispute.id)

    evidence = DisputeEvidence(
        dispute=dispute,
        user=request.user,
        title=title,
        description=description,
        evidence_type=evidence_type,
        file_attachment=file_attachment
    )

    try:
        evidence.full_clean()
        evidence.save()
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages) if hasattr(e, 'messages') else str(e))
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Notify counterparty
    counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"{request.user.username} submitted new evidence for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


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

        evidence_deadline = timezone.now() + timedelta(days=7)

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
                dispute.is_expired = False
                dispute.resolution_reason = None
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    evidence_deadline=evidence_deadline
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
