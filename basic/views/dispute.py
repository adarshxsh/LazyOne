from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import math

from ..models import Dispute, Task, Notification, RewardLedger, UserProfile


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    is_resolver = (dispute.resolved_by == request.user)
    is_appealer = (dispute.appealed_by == request.user)

    if not is_participant and not request.user.is_staff and not is_resolver and not is_appealer and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_appealable': dispute.is_appealable,
        'can_arbitrate': not is_participant and dispute.status == 'open',
        'can_resolve_staff': request.user.is_staff and dispute.status in ['open', 'appealed'],
        'time_remaining_for_appeal': dispute.time_remaining_for_appeal,
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
    if dispute.status != 'open':
        messages.error(request, "Only open disputes can be withdrawn.")
        return redirect('my_tasks')

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.resolved_at = timezone.now()
        dispute.resolved_by = request.user
        dispute.resolution_ruling = 'withdrawn'
        dispute.resolution_notes = 'Dispute withdrawn by raising user.'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by if request.user != task.posted_by else (task.taken_by or task.posted_by),
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')


def _revert_previous_resolution(dispute):
    task = dispute.task
    prev_ruling = dispute.resolution_ruling

    if prev_ruling == 'favor_poster':
        poster_profile = task.posted_by.userprofile
        deduction = min(poster_profile.rewards, task.reward)
        poster_profile.rewards -= deduction
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=-deduction,
            transaction_type='task_creation',
            description=f"Reversal of previous dispute resolution in poster's favor on task: '{task.title}'"
        )
    elif prev_ruling == 'favor_taker':
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            deduction = min(taker_profile.rewards, task.reward)
            taker_profile.rewards -= deduction
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=-deduction,
                transaction_type='dispute_forfeit',
                description=f"Reversal of previous dispute resolution in taker's favor on task: '{task.title}'"
            )
    elif prev_ruling == 'split':
        poster_share = math.floor(task.reward / 2)
        taker_share = task.reward - poster_share

        if poster_share > 0:
            poster_profile = task.posted_by.userprofile
            deduction = min(poster_profile.rewards, poster_share)
            poster_profile.rewards -= deduction
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=-deduction,
                transaction_type='task_creation',
                description=f"Reversal of previous split resolution poster share on task: '{task.title}'"
            )

        if taker_share > 0 and task.taken_by:
            taker_profile = task.taken_by.userprofile
            deduction = min(taker_profile.rewards, taker_share)
            taker_profile.rewards -= deduction
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=-deduction,
                transaction_type='dispute_forfeit',
                description=f"Reversal of previous split resolution taker share on task: '{task.title}'"
            )


def _apply_dispute_resolution(dispute, resolver_user, ruling, notes, is_staff=False):
    task = dispute.task

    if ruling == 'favor_poster':
        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += task.reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=task.reward,
            transaction_type='task_cancellation',
            description=f"Refund from dispute resolution in poster's favor on task: '{task.title}'"
        )

        if dispute.escrow_status == 'held':
            dispute.forfeit_deposit(
                beneficiary=task.posted_by,
                reason_description=f"Security deposit bond forfeited to poster for dispute on task: '{task.title}'"
            )

        task.status = 'cancelled'
        task.save()

    elif ruling == 'favor_taker':
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Reward awarded from dispute resolution in taker's favor on task: '{task.title}'"
            )

        if dispute.escrow_status == 'held':
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for dispute resolved in taker's favor on task: '{task.title}'"
            )

        task.status = 'completed'
        task.save()

    elif ruling == 'split':
        poster_share = math.floor(task.reward / 2)
        taker_share = task.reward - poster_share

        if poster_share > 0:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += poster_share
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_share,
                transaction_type='task_cancellation',
                description=f"Partial refund from split dispute resolution on task: '{task.title}'"
            )

        if taker_share > 0 and task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += taker_share
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_share,
                transaction_type='task_completion',
                description=f"Partial reward from split dispute resolution on task: '{task.title}'"
            )

        if dispute.escrow_status == 'held':
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded from split dispute resolution on task: '{task.title}'"
            )

        task.status = 'cancelled'
        task.save()

    dispute.status = 'resolved'
    dispute.resolved_at = timezone.now()
    dispute.resolved_by = resolver_user
    dispute.resolution_ruling = ruling
    dispute.resolution_notes = notes
    dispute.save()

    dispute_link = reverse('dispute_detail', args=[dispute.id])
    role_str = "Staff" if is_staff else "Arbitrator"

    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{role_str} resolved dispute for task '{task.title}' with ruling: {ruling.replace('_', ' ').title()}.",
        link=dispute_link
    )
    if task.taken_by and task.taken_by != task.posted_by:
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"{role_str} resolved dispute for task '{task.title}' with ruling: {ruling.replace('_', ' ').title()}.",
            link=dispute_link
        )


@login_required(login_url='/login/')
@require_POST
def staff_resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Staff authorization required to resolve disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status not in ['open', 'appealed']:
        messages.error(request, "Dispute is not open for resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    ruling = request.POST.get('ruling')
    if ruling not in ['favor_poster', 'favor_taker', 'split']:
        messages.error(request, "Invalid resolution ruling selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    notes = request.POST.get('resolution_notes', request.POST.get('notes', '')).strip()

    with transaction.atomic():
        if dispute.status == 'appealed':
            _revert_previous_resolution(dispute)

        _apply_dispute_resolution(dispute, request.user, ruling, notes, is_staff=True)

    messages.success(request, f"Dispute resolved successfully by staff with ruling: {ruling.replace('_', ' ').title()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def arbitrate_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Parties involved in the dispute cannot act as neutral arbitrators.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for neutral arbitration.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    ruling = request.POST.get('ruling')
    if ruling not in ['favor_poster', 'favor_taker', 'split']:
        messages.error(request, "Invalid arbitration decision selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    notes = request.POST.get('resolution_notes', request.POST.get('notes', '')).strip()

    with transaction.atomic():
        _apply_dispute_resolution(dispute, request.user, ruling, notes, is_staff=False)

    messages.success(request, f"Neutral arbitration decision recorded: {ruling.replace('_', ' ').title()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can appeal a dispute resolution.")
        return redirect('home')

    if dispute.status != 'resolved':
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.resolved_at:
        messages.error(request, "Dispute does not have a valid resolution timestamp.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if timezone.now() > dispute.resolved_at + timedelta(hours=48):
        messages.error(request, "The 48-hour post-resolution appeal window has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.appealed_at is not None:
        messages.error(request, "An appeal has already been submitted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_reason = request.POST.get('appeal_reason', '').strip()
    if not appeal_reason:
        messages.error(request, "Please provide a reason for appealing the dispute resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.status = 'appealed'
        dispute.appealed_by = request.user
        dispute.appealed_at = timezone.now()
        dispute.appeal_reason = appeal_reason
        dispute.save()

        opposing_party = task.posted_by if request.user == task.taken_by else task.taken_by
        if opposing_party:
            Notification.objects.create(
                recipient=opposing_party,
                message=f"{request.user.username} has submitted an appeal for dispute on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        from django.contrib.auth.models import User
        staff_users = User.objects.filter(is_staff=True)
        for staff in staff_users:
            if staff != request.user:
                Notification.objects.create(
                    recipient=staff,
                    message=f"New appeal submitted by {request.user.username} for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    messages.success(request, "Your appeal has been submitted successfully and will be reviewed by staff.")
    return redirect('dispute_detail', dispute_id=dispute.id)
