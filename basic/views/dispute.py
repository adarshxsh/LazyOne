from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    dispute.check_auto_transition()

    evidence_entries = dispute.evidence_entries.all().order_by('created_at')

    can_submit_evidence = (dispute.status == 'evidence_pending') and (
        request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff
    )
    can_withdraw = (dispute.status == 'evidence_pending') and (request.user == dispute.raised_by or request.user.is_staff)
    can_escalate = (dispute.status == 'evidence_pending') and (
        request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff
    )
    can_resolve = (dispute.status == 'under_review' or (dispute.status == 'evidence_pending' and request.user == task.posted_by)) or request.user.is_staff

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_entries': evidence_entries,
        'can_submit_evidence': can_submit_evidence,
        'can_withdraw': can_withdraw,
        'can_escalate': can_escalate,
        'can_resolve': can_resolve,
    }
    return render(request, 'dispute_detail.html', context)

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
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason, status='evidence_pending')
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. Status: Evidence Pending.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully. Status set to Evidence Pending.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    if dispute.status != 'evidence_pending':
        messages.error(request, "Evidence can only be submitted during the evidence pending phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description')
    attachment_link = request.POST.get('attachment_link')

    if not description or not description.strip():
        messages.error(request, "Description is required for evidence submission.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        description=description.strip(),
        attachment_link=attachment_link.strip() if attachment_link else None
    )

    # Notify counterparty
    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"New evidence submitted by {request.user.username} for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_for_review(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to escalate this dispute.")
        return redirect('home')

    try:
        dispute.transition_to('under_review', user=request.user)
        for participant in [task.posted_by, task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Dispute for '{task.title}' has been moved to Under Review.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        messages.success(request, "Dispute status updated to Under Review.")
    except ValueError as e:
        messages.error(request, str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to resolve this dispute.")
        return redirect('home')

    is_mutual = (dispute.status == 'evidence_pending' and request.user == task.posted_by)
    try:
        with transaction.atomic():
            dispute.transition_to('resolved', user=request.user, is_mutual=is_mutual)
            task.status = 'completed'
            task.save()

            task_doer_profile = task.taken_by.userprofile
            task_doer_profile.rewards += task.reward
            task_doer_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by, task=task, amount=task.reward,
                transaction_type='task_completion', description=f"Completed task (Dispute Resolved): '{task.title}'"
            )

            for participant in [task.posted_by, task.taken_by]:
                if participant:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has been resolved.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
            messages.success(request, f"Dispute resolved and {task.reward} points awarded to {task.taken_by.username}.")
    except ValueError as e:
        messages.error(request, f"Cannot resolve dispute: {e}")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.raised_by != request.user and not request.user.is_staff:
        messages.error(request, "You can only withdraw disputes that you raised.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        dispute.transition_to('withdrawn', user=request.user)
        task = dispute.task
        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
        messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    except ValueError as e:
        messages.error(request, f"Cannot withdraw dispute: {e}")

    return redirect('my_tasks')
