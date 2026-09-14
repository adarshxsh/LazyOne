from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger
from ..forms import DisputeEvidenceForm

def process_expired_disputes():
    now = timezone.now()
    expired_disputes = Dispute.objects.filter(
        status__in=['open', 'under_review']
    ).filter(
        Q(expires_at__lte=now) | Q(evidence_deadline__lte=now)
    )

    processed_count = 0
    for dispute in expired_disputes:
        task = dispute.task
        with transaction.atomic():
            poster_evidences = dispute.evidence_entries.filter(submitted_by=task.posted_by).exists()
            doer_evidences = dispute.evidence_entries.filter(submitted_by=task.taken_by).exists() if task.taken_by else False

            if doer_evidences and not poster_evidences:
                if task.taken_by:
                    doer_profile = task.taken_by.userprofile
                    doer_profile.rewards += task.reward
                    doer_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Reward awarded for expired dispute on task: '{task.title}'"
                    )
                task.status = 'completed'
                outcome_text = f"Escrowed reward of {task.reward} points was awarded to {task.taken_by.username if task.taken_by else 'the task doer'}."
            else:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for expired dispute on task: '{task.title}'"
                )
                task.status = 'cancelled'
                outcome_text = f"Escrowed reward of {task.reward} points was refunded to {task.posted_by.username}."

            task.save()
            dispute.status = 'expired'
            dispute.save()

            notif_link = reverse('dispute_detail', args=[dispute.id])
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' has expired. {outcome_text}",
                link=notif_link
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' has expired. {outcome_text}",
                    link=notif_link
                )
            processed_count += 1
    return processed_count

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    process_expired_disputes()
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    now = timezone.now()
    is_active_status = dispute.status in ['open', 'under_review']
    is_evidence_window_active = is_active_status and bool(dispute.evidence_deadline) and (now < dispute.evidence_deadline)
    can_submit_evidence = (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff) and is_evidence_window_active

    remaining_seconds = 0
    if is_evidence_window_active and dispute.evidence_deadline:
        remaining_seconds = max(0, int((dispute.evidence_deadline - now).total_seconds()))

    evidence_entries = dispute.evidence_entries.all().order_by('created_at')
    form = DisputeEvidenceForm()

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_entries': evidence_entries,
        'form': form,
        'can_submit_evidence': can_submit_evidence,
        'is_evidence_window_active': is_evidence_window_active,
        'remaining_seconds': remaining_seconds,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    process_expired_disputes()
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    now = timezone.now()
    if dispute.status not in ['open', 'under_review'] or (dispute.evidence_deadline and now >= dispute.evidence_deadline):
        messages.error(request, "The evidence submission window for this dispute is closed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    form = DisputeEvidenceForm(request.POST, request.FILES)
    if form.is_valid():
        evidence = form.save(commit=False)
        evidence.dispute = dispute
        evidence.submitted_by = request.user
        evidence.save()

        if dispute.status == 'open':
            dispute.status = 'under_review'
            dispute.save()

        recipient = task.taken_by if request.user == task.posted_by else task.posted_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} uploaded new evidence for the dispute on '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, "Evidence submitted successfully.")
    else:
        for error_list in form.errors.values():
            for error in error_list:
                messages.error(request, error)

    return redirect('dispute_detail', dispute_id=dispute.id)

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
        now = timezone.now()
        dispute = Dispute.objects.create(
            task=task,
            raised_by=request.user,
            reason=reason,
            status='open',
            evidence_deadline=now + timedelta(hours=24),
            expires_at=now + timedelta(hours=48)
        )
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

