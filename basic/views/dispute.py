from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, DisputeEvidence, Task, Notification, RewardLedger, User
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidences = dispute.evidences.all().order_by('created_at')
    can_submit_evidence = dispute.can_submit_evidence(request.user)
    can_withdraw = dispute.can_withdraw(request.user)
    can_arbitrate = dispute.can_arbitrate(request.user)

    context = {
        'dispute': dispute,
        'task': task,
        'evidences': evidences,
        'can_submit_evidence': can_submit_evidence,
        'can_withdraw': can_withdraw,
        'can_arbitrate': can_arbitrate,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'evidence_submission', 'under_review']:
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
                    escrow_status='held',
                    status='open'
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
def start_evidence_collection(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to perform this action.")
        return redirect('home')

    if dispute.status == 'open':
        dispute.status = 'evidence_submission'
        dispute.save()

        Notification.objects.create(
            recipient=task.posted_by if request.user == task.taken_by else task.taken_by,
            message=f"Evidence collection phase has started for dispute on '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute moved to evidence submission phase.")
    else:
        messages.info(request, f"Dispute is currently in status: {dispute.get_status_display()}.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task parties can submit evidence.")
        return redirect('home')

    if dispute.status == 'open':
        dispute.status = 'evidence_submission'
        dispute.save()

    if dispute.status != 'evidence_submission':
        messages.error(request, "Evidence submission is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '').strip()
    attachment = request.FILES.get('attachment')

    if not description and not attachment:
        messages.error(request, "Please provide written claims or attach a file as evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=request.user,
            description=description,
            attachment=attachment
        )

        has_poster_evidence = dispute.evidences.filter(submitted_by=task.posted_by).exists()
        has_taker_evidence = dispute.evidences.filter(submitted_by=task.taken_by).exists()

        other_user = task.posted_by if request.user == task.taken_by else task.taken_by

        if has_poster_evidence and has_taker_evidence:
            dispute.status = 'under_review'
            dispute.save()
            msg = f"Both parties have submitted evidence for dispute on '{task.title}'. It is now under staff review."
            Notification.objects.create(
                recipient=task.posted_by,
                message=msg,
                link=reverse('dispute_detail', args=[dispute.id])
            )
            Notification.objects.create(
                recipient=task.taken_by,
                message=msg,
                link=reverse('dispute_detail', args=[dispute.id])
            )
            messages.success(request, "Evidence submitted successfully. Since both parties have submitted evidence, the dispute is now under review by staff.")
        else:
            Notification.objects.create(
                recipient=other_user,
                message=f"{request.user.username} has submitted evidence for dispute on '{task.title}'.",
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
        messages.error(request, "You are not authorized to perform this action.")
        return redirect('home')

    if dispute.status in ['open', 'evidence_submission']:
        dispute.status = 'under_review'
        dispute.save()

        msg = f"Dispute for task '{task.title}' has been submitted for staff review."
        Notification.objects.create(
            recipient=task.posted_by,
            message=msg,
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=task.taken_by,
            message=msg,
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute successfully submitted for staff review.")
    else:
        messages.error(request, "Dispute cannot be moved to review in its current state.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task

    if dispute.status not in ['open', 'evidence_submission']:
        messages.error(request, "Disputes cannot be withdrawn once under review or resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        other_user = task.posted_by if request.user == task.taken_by else task.taken_by
        Notification.objects.create(
            recipient=other_user,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now back in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def arbitrate_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff moderators can arbitrate disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'under_review':
        messages.error(request, "Only disputes under review can be arbitrated by staff.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('decision')
    notes = request.POST.get('notes', '').strip()

    if decision not in ['resolve_taker', 'resolve_poster']:
        messages.error(request, "Invalid arbitration decision.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if decision == 'resolve_taker':
            # Ruling in favor of Task Taker
            # 1. Award task reward to taker
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Awarded task reward by staff arbitration for task: '{task.title}'"
            )
            task.status = 'completed'
            task.save()

            # 2. Handle escrow deposit bond
            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded following staff ruling in favor of taker for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.taken_by,
                    reason_description=f"Security deposit bond forfeited to taker following staff ruling for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            ruling_msg = f"Staff arbitration ruled in favor of {task.taken_by.username} for task '{task.title}'."

        elif decision == 'resolve_poster':
            # Ruling in favor of Task Poster
            # 1. Refund task reward to poster
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refunded task reward by staff arbitration for task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

            # 2. Handle escrow deposit bond
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded following staff ruling in favor of poster for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited to poster following staff ruling for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            ruling_msg = f"Staff arbitration ruled in favor of {task.posted_by.username} for task '{task.title}'."

        if notes:
            ruling_msg += f" Moderator notes: {notes}"

        Notification.objects.create(
            recipient=task.posted_by,
            message=ruling_msg,
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=task.taken_by,
            message=ruling_msg,
            link=reverse('dispute_detail', args=[dispute.id])
        )

        messages.success(request, f"Dispute arbitrated successfully. Status set to resolved.")

    return redirect('dispute_detail', dispute_id=dispute.id)

