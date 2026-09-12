from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from datetime import timedelta
from ..models import Dispute, Task, Notification, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    can_resolve = request.user.is_staff and dispute.status == 'open'
    can_appeal = dispute.is_appealable and (request.user == task.posted_by or request.user == task.taken_by)
    can_review_appeal = request.user.is_staff and dispute.appeal_status == 'pending'

    context = {
        'dispute': dispute,
        'task': task,
        'can_resolve': can_resolve,
        'can_appeal': can_appeal,
        'can_review_appeal': can_review_appeal,
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
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
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

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Staff privileges required to adjudicate disputes.")
        return HttpResponseForbidden("Staff privileges required.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for adjudication.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    ruling = request.POST.get('ruling')
    resolution_notes = request.POST.get('resolution_notes', '').strip()

    if not ruling or ruling not in ['payout_taker', 'refund_poster', 'split']:
        messages.error(request, "Invalid ruling selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if ruling == 'payout_taker':
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_payout',
                description=f"Dispute payout for task: '{task.title}'"
            )
            task.status = 'completed'
            task.save()

        elif ruling == 'refund_poster':
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_refund',
                description=f"Dispute refund for task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

        elif ruling == 'split':
            try:
                poster_amount = int(request.POST.get('poster_amount', task.reward // 2))
                taker_amount = int(request.POST.get('taker_amount', task.reward - poster_amount))
            except (ValueError, TypeError):
                poster_amount = task.reward // 2
                taker_amount = task.reward - poster_amount

            if poster_amount + taker_amount != task.reward or poster_amount < 0 or taker_amount < 0:
                poster_amount = task.reward // 2
                taker_amount = task.reward - poster_amount

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += poster_amount
            poster_profile.save()

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += taker_amount
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_amount,
                transaction_type='dispute_split',
                description=f"Dispute split refund for task: '{task.title}'"
            )
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_amount,
                transaction_type='dispute_split',
                description=f"Dispute split payout for task: '{task.title}'"
            )
            task.status = 'completed'
            task.save()

        dispute.status = 'resolved'
        dispute.resolution_ruling = ruling
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' has been adjudicated (Ruling: {ruling}).",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' has been adjudicated (Ruling: {ruling}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute for '{task.title}' successfully adjudicated.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only the task poster or taker can appeal this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'resolved':
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.appeal_status and dispute.appeal_status != 'none':
        messages.error(request, "An appeal has already been submitted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.resolved_at or timezone.now() > dispute.resolved_at + timedelta(days=7):
        messages.error(request, "The 7-day appeal window for this dispute has passed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_reason = request.POST.get('appeal_reason') or request.POST.get('reason')
    if not appeal_reason or not appeal_reason.strip():
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.appeal_status = 'pending'
        dispute.appeal_reason = appeal_reason.strip()
        dispute.appeal_filed_by = request.user
        dispute.appeal_filed_at = timezone.now()
        dispute.status = 'under_appeal'
        dispute.save()

        other_user = task.taken_by if request.user == task.posted_by else task.posted_by
        if other_user:
            Notification.objects.create(
                recipient=other_user,
                message=f"An appeal has been filed for the dispute on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        staff_members = User.objects.filter(is_staff=True)
        for staff in staff_members:
            Notification.objects.create(
                recipient=staff,
                message=f"New dispute appeal submitted for task '{task.title}' by {request.user.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Your appeal has been submitted successfully and is pending administrative review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Staff privileges required to review appeals.")
        return HttpResponseForbidden("Staff privileges required.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.appeal_status != 'pending':
        messages.error(request, "This dispute does not have a pending appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    action = request.POST.get('action')
    notes = request.POST.get('notes', '').strip() or request.POST.get('appeal_resolution_notes', '').strip()
    corrective_action = request.POST.get('corrective_action')

    if not action or action not in ['approve', 'overturn', 'reject', 'confirm']:
        messages.error(request, "Invalid appeal review action.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if action in ['reject', 'confirm']:
            dispute.appeal_status = 'rejected'
            dispute.status = 'appeal_closed'
            dispute.appeal_resolution_notes = notes
            dispute.save()
        else:
            dispute.appeal_status = 'approved'
            dispute.status = 'appeal_closed'
            dispute.appeal_resolution_notes = notes

            if corrective_action:
                if corrective_action == 'refund_poster' and dispute.resolution_ruling == 'payout_taker':
                    taker_profile = task.taken_by.userprofile
                    poster_profile = task.posted_by.userprofile
                    taker_profile.rewards -= task.reward
                    taker_profile.save()
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=-task.reward,
                        transaction_type='appeal_refund',
                        description=f"Appeal correction: reversed payout for task '{task.title}'"
                    )
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='appeal_refund',
                        description=f"Appeal correction: refunded for task '{task.title}'"
                    )
                    task.status = 'cancelled'
                    task.save()

                elif corrective_action == 'payout_taker' and dispute.resolution_ruling == 'refund_poster':
                    poster_profile = task.posted_by.userprofile
                    taker_profile = task.taken_by.userprofile
                    poster_profile.rewards -= task.reward
                    poster_profile.save()
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=-task.reward,
                        transaction_type='appeal_payout',
                        description=f"Appeal correction: reversed refund for task '{task.title}'"
                    )
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='appeal_payout',
                        description=f"Appeal correction: paid out for task '{task.title}'"
                    )
                    task.status = 'completed'
                    task.save()

            dispute.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"The appeal for dispute on task '{task.title}' has been reviewed ({dispute.appeal_status}).",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"The appeal for dispute on task '{task.title}' has been reviewed ({dispute.appeal_status}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appeal review for '{task.title}' finalized.")
    return redirect('dispute_detail', dispute_id=dispute.id)
