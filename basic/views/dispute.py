from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    now = timezone.now()
    is_expired = False
    if dispute.resolved_at:
        is_expired = (now - dispute.resolved_at) > timedelta(days=7)

    can_appeal = (
        dispute.status == 'resolved' and
        dispute.resolved_at is not None and
        not is_expired and
        dispute.appeal_status in ['none', None, ''] and
        (request.user == task.posted_by or request.user == task.taken_by)
    )

    context = {
        'dispute': dispute,
        'task': task,
        'can_appeal': can_appeal,
        'is_expired': is_expired,
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

@login_required(login_url='/login/')
def admin_dispute_queue(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")
    
    open_disputes = Dispute.objects.filter(status='open').order_by('-created_at')
    pending_appeals = Dispute.objects.filter(appeal_status='appealed').order_by('-appeal_created_at')

    context = {
        'open_disputes': open_disputes,
        'pending_appeals': pending_appeals,
    }
    return render(request, 'admin_dispute_queue.html', context)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")
    
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for initial resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('resolution_decision') or request.POST.get('decision')
    notes = request.POST.get('resolution_notes') or request.POST.get('notes', '')

    if decision not in ['favor_poster', 'favor_taker', 'split_50_50']:
        messages.error(request, "Invalid resolution decision.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    with transaction.atomic():
        if dispute.escrow_status == 'held':
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon dispute arbitration for task: '{task.title}'"
            )

        dispute.status = 'resolved'
        dispute.resolution_decision = decision
        dispute.resolved_by = request.user
        dispute.resolution_notes = notes
        dispute.resolved_at = timezone.now()
        dispute.save()

        if decision == 'favor_poster':
            task.status = 'cancelled'
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Dispute resolved in favor of poster for task: '{task.title}'"
            )
        elif decision == 'favor_taker':
            task.status = 'completed'
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Dispute resolved in favor of taker for task: '{task.title}'"
            )
        elif decision == 'split_50_50':
            task.status = 'completed'
            poster_share = task.reward // 2
            taker_share = task.reward - poster_share
            
            if poster_share > 0:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += poster_share
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_share,
                    transaction_type='dispute_resolution',
                    description=f"Dispute split resolution (refund 50%) for task: '{task.title}'"
                )
            if taker_share > 0:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += taker_share
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_share,
                    transaction_type='dispute_resolution',
                    description=f"Dispute split resolution (award 50%) for task: '{task.title}'"
                )
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved with outcome: {decision}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved with outcome: {decision}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute resolved successfully with decision: {decision}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        return HttpResponseForbidden("You are not authorized to appeal this dispute.")

    if dispute.status != 'resolved' or dispute.resolved_at is None:
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.appeal_status in ['appealed', 'appeal_upheld', 'appeal_rejected']:
        messages.error(request, "An appeal has already been submitted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    now = timezone.now()
    if (now - dispute.resolved_at) > timedelta(days=7):
        messages.error(request, "Appeals must be submitted within 7 days of resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('appeal_reason') or request.POST.get('reason')
    if not reason:
        messages.error(request, "An appeal justification is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.appeal_status = 'appealed'
    dispute.status = 'appealed'
    dispute.appealed_by = request.user
    dispute.appeal_reason = reason
    dispute.appeal_created_at = now
    dispute.save()

    other_user = task.taken_by if request.user == task.posted_by else task.posted_by
    if other_user:
        Notification.objects.create(
            recipient=other_user,
            message=f"An appeal has been submitted for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Appeal submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_appeal(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.appeal_status != 'appealed':
        messages.error(request, "This dispute does not have a pending appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    action = request.POST.get('action') # 'uphold' or 'reverse'
    new_decision = request.POST.get('resolution_decision') or request.POST.get('new_decision') or request.POST.get('decision')
    notes = request.POST.get('appeal_notes') or request.POST.get('notes', '')

    task = dispute.task
    now = timezone.now()

    with transaction.atomic():
        if action == 'uphold' or new_decision == dispute.resolution_decision:
            dispute.appeal_status = 'appeal_rejected'
            dispute.status = 'appeal_resolved'
            dispute.appeal_resolved_by = request.user
            dispute.appeal_resolved_at = now
            dispute.appeal_notes = notes
            dispute.save()
            messages.success(request, "Initial ruling upheld successfully.")
        else:
            if new_decision not in ['favor_poster', 'favor_taker', 'split_50_50']:
                messages.error(request, "Invalid resolution decision for appeal reversal.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            def get_payouts(decision_code, reward):
                if decision_code == 'favor_poster':
                    return reward, 0
                elif decision_code == 'favor_taker':
                    return 0, reward
                elif decision_code == 'split_50_50':
                    p_share = reward // 2
                    return p_share, reward - p_share
                return 0, 0

            init_p, init_t = get_payouts(dispute.resolution_decision, task.reward)
            new_p, new_t = get_payouts(new_decision, task.reward)

            poster_delta = new_p - init_p
            taker_delta = new_t - init_t

            if poster_delta != 0:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += poster_delta
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_delta,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment for task: '{task.title}'"
                )

            if taker_delta != 0 and task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += taker_delta
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_delta,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment for task: '{task.title}'"
                )

            task.status = 'cancelled' if new_decision == 'favor_poster' else 'completed'
            task.save()

            dispute.resolution_decision = new_decision
            dispute.appeal_status = 'appeal_upheld'
            dispute.status = 'appeal_resolved'
            dispute.appeal_resolved_by = request.user
            dispute.appeal_resolved_at = now
            dispute.appeal_notes = notes
            dispute.save()
            messages.success(request, f"Appeal resolved: Decision updated to {new_decision}.")

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"The appeal for dispute on task '{task.title}' has been resolved.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"The appeal for dispute on task '{task.title}' has been resolved.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return redirect('dispute_detail', dispute_id=dispute.id)
