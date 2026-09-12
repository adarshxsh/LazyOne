from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from datetime import timedelta
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and (task.taken_by and request.user != task.taken_by) and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    is_appeal_window_active = False
    if dispute.status == 'resolved' and dispute.resolved_at:
        is_appeal_window_active = (timezone.now() - dispute.resolved_at) <= timedelta(days=3)

    context = {
        'dispute': dispute,
        'task': task,
        'is_appeal_window_active': is_appeal_window_active,
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
        messages.error(request, "Only staff arbitrators can resolve disputes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "Only open disputes can be resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    resolution_type = request.POST.get('resolution_type')
    resolution_notes = request.POST.get('resolution_notes', '').strip()

    if resolution_type not in ['poster_wins', 'taker_wins', 'split']:
        messages.error(request, "Invalid resolution type.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolution_type = resolution_type
        dispute.resolved_by = request.user
        dispute.resolution_notes = resolution_notes
        dispute.resolved_at = timezone.now()
        dispute.save()

        if resolution_type == 'poster_wins':
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Dispute resolution refund for task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

        elif resolution_type == 'taker_wins':
            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Dispute resolution payout for task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()

        elif resolution_type == 'split':
            poster_amount = task.reward // 2
            taker_amount = task.reward - poster_amount

            poster_profile.rewards += poster_amount
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_amount,
                transaction_type='dispute_resolution',
                description=f"Dispute split refund for task: '{task.title}'"
            )

            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += taker_amount
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_amount,
                    transaction_type='dispute_resolution',
                    description=f"Dispute split payout for task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()

        res_display = dispute.get_resolution_type_display()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved: {res_display}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved: {res_display}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute resolved successfully ({dispute.get_resolution_type_display()}).")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'resolved' or dispute.appeal_status != 'none':
        messages.error(request, "This dispute cannot be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.resolved_at and (timezone.now() - dispute.resolved_at > timedelta(days=3)):
        messages.error(request, "The appeal window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_reason = request.POST.get('appeal_reason', '').strip()
    if not appeal_reason:
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.status = 'appealed'
        dispute.appeal_status = 'appealed'
        dispute.appealed_by = request.user
        dispute.appeal_reason = appeal_reason
        dispute.appealed_at = timezone.now()
        dispute.save()

        other_user = task.taken_by if request.user == task.posted_by else task.posted_by
        if other_user:
            Notification.objects.create(
                recipient=other_user,
                message=f"{request.user.username} has submitted an appeal for dispute on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        staff_users = User.objects.filter(is_staff=True)
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"New dispute appeal submitted by {request.user.username} for task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Appeal submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can review appeals.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.appeal_status != 'appealed':
        messages.error(request, "Only pending appeals can be reviewed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    action = request.POST.get('action')
    appeal_notes = request.POST.get('appeal_notes', '').strip()
    new_resolution_type = request.POST.get('new_resolution_type')

    if action not in ['uphold', 'overturn']:
        messages.error(request, "Invalid appeal review action.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task

    with transaction.atomic():
        if action == 'uphold':
            dispute.status = 'resolved'
            dispute.appeal_status = 'upheld'
            dispute.appeal_reviewed_by = request.user
            dispute.appeal_notes = appeal_notes
            dispute.save()
            messages.success(request, "Appeal decision upheld.")
        else:
            target_resolution = new_resolution_type
            if target_resolution not in ['poster_wins', 'taker_wins', 'split']:
                if dispute.resolution_type == 'poster_wins':
                    target_resolution = 'taker_wins'
                elif dispute.resolution_type == 'taker_wins':
                    target_resolution = 'poster_wins'
                else:
                    target_resolution = 'poster_wins' if dispute.appealed_by == task.posted_by else 'taker_wins'

            old_poster_amount = 0
            old_taker_amount = 0
            if dispute.resolution_type == 'poster_wins':
                old_poster_amount = task.reward
            elif dispute.resolution_type == 'taker_wins':
                old_taker_amount = task.reward
            elif dispute.resolution_type == 'split':
                old_poster_amount = task.reward // 2
                old_taker_amount = task.reward - old_poster_amount

            new_poster_amount = 0
            new_taker_amount = 0
            if target_resolution == 'poster_wins':
                new_poster_amount = task.reward
            elif target_resolution == 'taker_wins':
                new_taker_amount = task.reward
            elif target_resolution == 'split':
                new_poster_amount = task.reward // 2
                new_taker_amount = task.reward - new_poster_amount

            poster_diff = new_poster_amount - old_poster_amount
            taker_diff = new_taker_amount - old_taker_amount

            if poster_diff != 0:
                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += poster_diff
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_diff,
                    transaction_type='dispute_reversal',
                    description=f"Appeal reversal adjustment for task: '{task.title}'"
                )

            if taker_diff != 0 and task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += taker_diff
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_diff,
                    transaction_type='dispute_reversal',
                    description=f"Appeal reversal adjustment for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.appeal_status = 'overturned'
            dispute.resolution_type = target_resolution
            dispute.appeal_reviewed_by = request.user
            dispute.appeal_notes = appeal_notes
            dispute.save()

            if target_resolution == 'poster_wins':
                task.status = 'cancelled'
            else:
                task.status = 'completed'
            task.save()
            messages.success(request, f"Appeal decision overturned to '{dispute.get_resolution_type_display()}'.")

        appeal_disp = dispute.get_appeal_status_display()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Appeal review for task '{task.title}': Decision {appeal_disp}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Appeal review for task '{task.title}': Decision {appeal_disp}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return redirect('dispute_detail', dispute_id=dispute.id)
