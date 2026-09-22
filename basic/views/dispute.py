from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeAuditEvent, DisputeEvidence, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = request.user in dispute.jurors.all()
    if request.user != task.posted_by and request.user != task.taken_by and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    audit_events = dispute.audit_events.all().order_by('timestamp')
    evidence_list = dispute.evidence_list.all()
    votes = dispute.votes.all()

    context = {
        'dispute': dispute,
        'task': task,
        'audit_events': audit_events,
        'evidence_list': evidence_list,
        'votes': votes,
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

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=request.user,
                event_type='dispute_created',
                details_json={
                    'reason': reason,
                    'deposit_amount': deposit_amount,
                    'raised_by': request.user.username
                }
            )

            dispute.notify_participants(
                message=f"{request.user.username} has raised a dispute for task: '{task.title}'."
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

        DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=request.user,
            event_type='dispute_resolved',
            details_json={
                'action': 'withdrawn',
                'status': 'resolved'
            }
        )

        dispute.notify_participants(
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress."
        )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    is_juror = request.user in dispute.jurors.all()
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('evidence') or request.POST.get('description', '')
    if not description:
        messages.error(request, "Evidence description cannot be empty.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=request.user,
            description=description
        )
        DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=request.user,
            event_type='evidence_submitted',
            details_json={'description': description}
        )
        dispute.notify_participants(
            message=f"{request.user.username} submitted evidence for dispute on '{task.title}'."
        )

    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    is_juror = request.user in dispute.jurors.all()
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for') or request.POST.get('voted_for_id')
    voted_for = get_object_or_404(User, id=voted_for_id) if voted_for_id else None

    with transaction.atomic():
        vote, created = DisputeVote.objects.update_or_create(
            dispute=dispute,
            voter=request.user,
            defaults={'voted_for': voted_for}
        )
        DisputeAuditEvent.objects.create(
            dispute=dispute,
            actor=request.user,
            event_type='vote_cast',
            details_json={
                'voter': request.user.username,
                'voted_for': voted_for.username if voted_for else None
            }
        )
        dispute.notify_participants(
            message=f"A vote was cast by {request.user.username} for dispute on '{task.title}'."
        )

    messages.success(request, "Vote recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)
