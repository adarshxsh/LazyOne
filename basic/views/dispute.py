from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from datetime import timedelta

from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence
from ..forms import DisputeEvidenceForm


def check_and_expire_dispute(dispute):
    """
    On-demand dispute expiration check.
    If dispute is open and current time exceeds dispute.deadline,
    auto-resolves the dispute, refunds/awards escrowed points,
    updates task status, logs RewardLedger transaction, and sends notifications.
    Returns True if dispute was expired and resolved, False otherwise.
    """
    if not dispute or dispute.status != 'open':
        return False

    if not dispute.deadline or timezone.now() < dispute.deadline:
        return False

    with transaction.atomic():
        try:
            dispute = Dispute.objects.select_for_update().get(id=dispute.id, status='open')
        except Dispute.DoesNotExist:
            return False

        if not dispute.deadline or timezone.now() < dispute.deadline:
            return False

        task = dispute.task

        if dispute.expiration_handler == 'award_taker' and task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            task.status = 'completed'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_payout',
                description=f"Automated dispute payout for completed task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for '{task.title}' expired. You have been awarded {task.reward} points.",
                link=reverse('my_tasks')
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' expired. Points awarded to task taker.",
                link=reverse('my_tasks')
            )
        else:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_refund',
                description=f"Automated dispute refund for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' expired. Your escrowed {task.reward} points have been refunded.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for '{task.title}' expired and has been resolved.",
                    link=reverse('my_tasks')
                )

        return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    # On-demand expiration check
    check_and_expire_dispute(dispute)
    dispute.refresh_from_db()

    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    if request.method == 'POST':
        if dispute.status != 'open':
            messages.error(request, "This dispute has been resolved. No further evidence can be submitted.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        form = DisputeEvidenceForm(request.POST, request.FILES)
        if form.is_valid():
            evidence = form.save(commit=False)
            evidence.dispute = dispute
            evidence.submitted_by = request.user
            evidence.save()

            counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} submitted new evidence for dispute on '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            messages.success(request, "Evidence submitted successfully.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        else:
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
    else:
        form = DisputeEvidenceForm()

    evidences = dispute.evidences.all().order_by('created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'form': form,
        'now': timezone.now(),
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute'):
        check_and_expire_dispute(task.dispute)
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

        if hasattr(task, 'dispute'):
            task.dispute.delete()

        deadline = timezone.now() + timedelta(days=3)
        dispute = Dispute.objects.create(
            task=task,
            raised_by=request.user,
            reason=reason,
            deadline=deadline
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
