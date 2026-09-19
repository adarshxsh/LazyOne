import math
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    can_appeal = False
    appeal_window_expires_at = None
    if dispute.status == 'arbitrated' and (request.user == task.posted_by or request.user == task.taken_by):
        if dispute.arbitrated_at:
            appeal_window_expires_at = dispute.arbitrated_at + timedelta(hours=72)
            if timezone.now() <= appeal_window_expires_at:
                can_appeal = True
        else:
            can_appeal = True

    context = {
        'dispute': dispute,
        'task': task,
        'can_appeal': can_appeal,
        'appeal_window_expires_at': appeal_window_expires_at,
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
def dispute_arbitrate(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff administrators can arbitrate disputes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, f"Dispute is not open for arbitration (current status: {dispute.get_status_display()}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.method == 'POST':
        ruling = request.POST.get('ruling', '').strip().lower()
        explanation = request.POST.get('explanation', '').strip() or request.POST.get('ruling_explanation', '').strip()

        if ruling not in ['taker', 'poster', 'split', 'favor_taker', 'favor_poster', 'partial_split']:
            messages.error(request, "Invalid arbitration ruling selected.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        task = dispute.task

        with transaction.atomic():
            dispute.status = 'arbitrated'
            dispute.arbitration_ruling = f"{ruling.replace('_', ' ').capitalize()}: {explanation}" if explanation else ruling
            dispute.arbitrated_by = request.user
            dispute.arbitrated_at = timezone.now()
            dispute.save()

            if ruling in ['taker', 'favor_taker']:
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_arbitration_payout',
                        description=f"Arbitration payout (Favor Taker) for task: '{task.title}'"
                    )
                if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                    if dispute.raised_by == task.taken_by:
                        dispute.refund_deposit(
                            reason_description=f"Security deposit bond refunded following arbitration on task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=task.taken_by,
                            reason_description=f"Security deposit bond forfeited to taker following arbitration on task: '{task.title}'"
                        )
                task.status = 'completed'
                task.save()

            elif ruling in ['poster', 'favor_poster']:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_arbitration_payout',
                    description=f"Arbitration refund (Favor Poster) for task: '{task.title}'"
                )
                if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                    if dispute.raised_by == task.posted_by:
                        dispute.refund_deposit(
                            reason_description=f"Security deposit bond refunded following arbitration on task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=task.posted_by,
                            reason_description=f"Security deposit bond forfeited to poster following arbitration on task: '{task.title}'"
                        )
                task.status = 'cancelled'
                task.save()

            else: # Split
                taker_share = math.floor(task.reward / 2)
                poster_share = task.reward - taker_share

                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += taker_share
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=taker_share,
                        transaction_type='dispute_arbitration_payout',
                        description=f"Arbitration partial split payout for task: '{task.title}'"
                    )

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += poster_share
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_share,
                    transaction_type='dispute_arbitration_payout',
                    description=f"Arbitration partial split refund for task: '{task.title}'"
                )

                if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded for split arbitration on task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' has been arbitrated. Status is now Arbitrated.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' has been arbitrated. Status is now Arbitrated.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Arbitration ruling issued successfully for task '{task.title}'.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'arbitrated':
        messages.error(request, f"Appeals can only be submitted for arbitrated disputes (current status: {dispute.get_status_display()}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.arbitrated_at:
        if timezone.now() > dispute.arbitrated_at + timedelta(hours=72):
            messages.error(request, "The 72-hour appeal window for this dispute has expired.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    if request.method == 'POST':
        appeal_reason = request.POST.get('appeal_reason', '').strip() or request.POST.get('reason', '').strip()
        if not appeal_reason:
            messages.error(request, "An appeal reason is required to submit an appeal.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        with transaction.atomic():
            dispute.status = 'under_appeal'
            dispute.appeal_reason = appeal_reason
            dispute.appealed_by = request.user
            dispute.appealed_at = timezone.now()
            dispute.save()

            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has submitted an appeal for dispute on task '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, "Your appeal has been submitted successfully and is now under staff review.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def resolve_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff administrators can resolve dispute appeals.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'under_appeal':
        messages.error(request, f"Dispute is not under appeal (current status: {dispute.get_status_display()}).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.method == 'POST':
        decision = request.POST.get('decision', '').strip().lower() or request.POST.get('ruling', '').strip().lower()
        notes = request.POST.get('notes', '').strip() or request.POST.get('explanation', '').strip()

        task = dispute.task

        with transaction.atomic():
            if decision in ['reject', 'uphold', 'appeal_rejected', 'reject_appeal']:
                dispute.status = 'appeal_rejected'
                dispute.save()
            else:
                initial_ruling_lower = (dispute.arbitration_ruling or '').lower()

                if decision in ['taker', 'favor_taker']:
                    new_ruling = 'taker'
                elif decision in ['poster', 'favor_poster']:
                    new_ruling = 'poster'
                elif decision in ['split', 'partial_split']:
                    new_ruling = 'split'
                else:
                    if 'poster' in initial_ruling_lower:
                        new_ruling = 'taker'
                    else:
                        new_ruling = 'poster'

                if 'poster' in initial_ruling_lower and new_ruling == 'taker':
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards = max(0, poster_profile.rewards - task.reward)
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=-task.reward,
                        transaction_type='dispute_appeal_adjustment',
                        description=f"Appeal adjustment debit for task: '{task.title}'"
                    )

                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='dispute_appeal_adjustment',
                            description=f"Appeal adjustment credit for task: '{task.title}'"
                        )
                    task.status = 'completed'
                    task.save()

                elif ('taker' in initial_ruling_lower or 'split' in initial_ruling_lower) and new_ruling == 'poster':
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards = max(0, taker_profile.rewards - task.reward)
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=-task.reward,
                            transaction_type='dispute_appeal_adjustment',
                            description=f"Appeal adjustment debit for task: '{task.title}'"
                        )

                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_appeal_adjustment',
                        description=f"Appeal adjustment credit for task: '{task.title}'"
                    )
                    task.status = 'cancelled'
                    task.save()

                dispute.status = 'resolved'
                dispute.arbitration_ruling = f"Appeal Upheld ({new_ruling.capitalize()}): {notes}" if notes else f"Appeal Upheld ({new_ruling.capitalize()})"
                dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"The appeal for dispute on task '{task.title}' has been resolved. Status: {dispute.get_status_display()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"The appeal for dispute on task '{task.title}' has been resolved. Status: {dispute.get_status_display()}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Appeal resolved successfully for task '{task.title}'.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('dispute_detail', dispute_id=dispute.id)
