from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JurorAssignment, DisputeAppeal
from basic.services.dispute import DisputeService
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_assigned_juror = dispute.juror_assignments.filter(juror=request.user).exists()

    if not is_participant and not is_assigned_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    active_assignment = dispute.juror_assignments.filter(juror=request.user, vote__isnull=True).first()
    can_appeal = dispute.can_appeal(request.user)
    appeal = getattr(dispute, 'appeal', None)

    context = {
        'dispute': dispute,
        'task': task,
        'active_assignment': active_assignment,
        'can_appeal': can_appeal,
        'appeal': appeal,
        'is_participant': is_participant,
        'is_juror': is_assigned_juror,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'voting']:
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
                dispute.primary_verdict = None
                dispute.final_verdict = None
                dispute.verdict_published_at = None
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
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

            # Assemble Tier 1 Jury Panel
            DisputeService.assemble_juror_panel(dispute, tier=1, count=3)

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
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    vote = request.POST.get('vote')

    assignment = JurorAssignment.objects.filter(dispute=dispute, juror=request.user, vote__isnull=True).first()
    if not assignment:
        messages.error(request, "You do not have an active juror vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        resolved, outcome = DisputeService.submit_juror_vote(dispute, request.user, vote, tier=assignment.tier)
        messages.success(request, f"Your vote for '{vote.capitalize()}' has been recorded.")
    except ValueError as e:
        messages.error(request, str(e))

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    reason = request.POST.get('reason')

    if not reason:
        messages.error(request, "An appeal statement/reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        appeal = DisputeService.file_appeal(dispute, request.user, reason)
        messages.success(request, f"Appeal filed successfully. Escrow locked and secondary jury assigned.")
    except ValueError as e:
        messages.error(request, str(e))

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
