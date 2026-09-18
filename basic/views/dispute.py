import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    can_appeal = dispute.is_appealable() and request.user in [task.posted_by, task.taken_by]

    context = {
        'dispute': dispute,
        'task': task,
        'can_appeal': can_appeal,
        'is_staff_user': request.user.is_staff,
        'is_participant': request.user in [task.posted_by, task.taken_by],
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
def admin_dispute_list_view(request):
    if not request.user.is_staff:
        messages.error(request, "Staff access required.")
        return redirect('home')

    filter_status = request.GET.get('status', 'all')
    if filter_status == 'open':
        disputes = Dispute.objects.filter(status='open')
    elif filter_status == 'pending_appeal':
        disputes = Dispute.objects.filter(status='pending_appeal')
    elif filter_status == 'resolved':
        disputes = Dispute.objects.filter(status__in=['resolved', 'appeal_closed', 'appeal_upheld', 'appeal_reversed'])
    else:
        disputes = Dispute.objects.all()

    disputes = disputes.order_by('-created_at')

    context = {
        'disputes': disputes,
        'filter_status': filter_status,
    }
    return render(request, 'admin_disputes.html', context)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff administrators can resolve disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for initial resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('decision')  # 'poster', 'taker', 'split'
    rationale = request.POST.get('rationale', '').strip()

    if decision not in ['poster', 'taker', 'split']:
        messages.error(request, "Invalid decision type selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not rationale:
        messages.error(request, "A decision rationale is required for administrative resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    total_reward = task.reward

    if decision == 'poster':
        poster_payout = total_reward
        taker_payout = 0
    elif decision == 'taker':
        poster_payout = 0
        taker_payout = total_reward
    else:  # 'split'
        taker_pct_input = request.POST.get('taker_percent') or request.POST.get('split_percent') or request.POST.get('percentage') or '50'
        try:
            taker_pct = int(taker_pct_input)
            taker_pct = max(0, min(100, taker_pct))
        except ValueError:
            taker_pct = 50

        taker_payout = math.floor(total_reward * taker_pct / 100)
        poster_payout = total_reward - taker_payout

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolved_by = request.user
        dispute.resolution_decision = decision
        dispute.poster_payout = poster_payout
        dispute.taker_payout = taker_payout
        dispute.resolution_rationale = rationale
        dispute.resolved_at = timezone.now()
        dispute.save()

        if poster_payout > 0 and task.posted_by:
            poster_profile, _ = UserProfile.objects.select_for_update().get_or_create(user=task.posted_by)
            poster_profile.rewards += poster_payout
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_payout,
                transaction_type='dispute_refund',
                description=f"Dispute resolution refund for task '{task.title}'"
            )

        if taker_payout > 0 and task.taken_by:
            taker_profile, _ = UserProfile.objects.select_for_update().get_or_create(user=task.taken_by)
            taker_profile.rewards += taker_payout
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_payout,
                transaction_type='dispute_payout',
                description=f"Dispute resolution payout for task '{task.title}'"
            )

        if decision == 'poster':
            task.status = 'cancelled'
        else:
            task.status = 'completed'
        task.save()

        if task.posted_by:
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' has been resolved by administrative staff.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for '{task.title}' has been resolved by administrative staff.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute resolved successfully with decision: {decision.capitalize()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def file_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can file an appeal.")
        return redirect('home')

    if not dispute.is_appealable():
        messages.error(request, "This dispute cannot be appealed (outside 72-hour window or already appealed).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    justification = request.POST.get('justification', '').strip() or request.POST.get('reason', '').strip()
    if not justification:
        messages.error(request, "A mandatory justification is required to file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.status = 'pending_appeal'
        dispute.appealed_by = request.user
        dispute.appeal_justification = justification
        dispute.appealed_at = timezone.now()
        dispute.save()

        other_party = task.taken_by if request.user == task.posted_by else task.posted_by
        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"An appeal has been filed by {request.user.username} for dispute on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Appeal submitted successfully and is now pending staff review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff administrators can review dispute appeals.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'pending_appeal':
        messages.error(request, "This dispute is not currently pending an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_decision = request.POST.get('appeal_decision', '').lower()
    rationale = request.POST.get('rationale', '').strip()

    if appeal_decision not in ['uphold', 'reverse', 'upheld', 'reversed', 'approved', 'rejected']:
        messages.error(request, "Invalid appeal decision choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not rationale:
        messages.error(request, "A rationale is required for appellate decision.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task

    with transaction.atomic():
        if appeal_decision in ['uphold', 'upheld', 'rejected']:
            dispute.status = 'appeal_closed'
            dispute.appeal_decision = 'uphold'
            dispute.appeal_rationale = rationale
            dispute.appeal_resolved_by = request.user
            dispute.appeal_resolved_at = timezone.now()
            dispute.save()
        else:  # reverse
            old_poster = dispute.poster_payout
            old_taker = dispute.taker_payout

            new_poster = old_taker
            new_taker = old_poster

            poster_delta = new_poster - old_poster
            taker_delta = new_taker - old_taker

            if poster_delta != 0 and task.posted_by:
                poster_profile, _ = UserProfile.objects.select_for_update().get_or_create(user=task.posted_by)
                poster_profile.rewards += poster_delta
                poster_profile.save()
                trans_type = 'appeal_refund' if poster_delta > 0 else 'appeal_reversal'
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_delta,
                    transaction_type=trans_type,
                    description=f"Appeal reversal adjustment for task '{task.title}'"
                )

            if taker_delta != 0 and task.taken_by:
                taker_profile, _ = UserProfile.objects.select_for_update().get_or_create(user=task.taken_by)
                taker_profile.rewards += taker_delta
                taker_profile.save()
                trans_type = 'appeal_payout' if taker_delta > 0 else 'appeal_reversal'
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_delta,
                    transaction_type=trans_type,
                    description=f"Appeal reversal adjustment for task '{task.title}'"
                )

            dispute.poster_payout = new_poster
            dispute.taker_payout = new_taker
            dispute.status = 'appeal_closed'
            dispute.appeal_decision = 'reverse'
            dispute.appeal_rationale = rationale
            dispute.appeal_resolved_by = request.user
            dispute.appeal_resolved_at = timezone.now()
            dispute.save()

            if new_poster == task.reward:
                task.status = 'cancelled'
            else:
                task.status = 'completed'
            task.save()

        if task.posted_by:
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Appeal for task '{task.title}' finalized by admin: {dispute.appeal_decision.capitalize()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Appeal for task '{task.title}' finalized by admin: {dispute.appeal_decision.capitalize()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appellate decision saved: Decision {dispute.appeal_decision.capitalize()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)
