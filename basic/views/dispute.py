from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.db import transaction
from django.utils import timezone
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
    context = {
        'dispute': dispute,
        'task': task
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
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Permission denied. Only authorized staff members can resolve disputes.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for resolution.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.method == 'POST':
        outcome = request.POST.get('resolution_outcome') or request.POST.get('outcome')
        notes = request.POST.get('resolution_notes', '').strip()

        if outcome not in ['poster_favored', 'taker_favored', 'split']:
            messages.error(request, "Invalid resolution outcome choice.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        task = dispute.task
        posted_by = task.posted_by
        taken_by = task.taken_by

        with transaction.atomic():
            dispute.status = 'resolved'
            dispute.resolution_outcome = outcome
            dispute.resolved_by = request.user
            dispute.resolved_at = timezone.now()
            dispute.resolution_notes = notes
            dispute.save()

            if outcome == 'poster_favored':
                poster_profile = posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Dispute resolution refund for task: '{task.title}'"
                )
                task.status = 'cancelled'
                task.save()

            elif outcome == 'taker_favored':
                if taken_by:
                    taker_profile = taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_resolution',
                        description=f"Dispute resolution award for task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()

            elif outcome == 'split':
                poster_share = task.reward // 2
                taker_share = task.reward - poster_share

                poster_profile = posted_by.userprofile
                poster_profile.rewards += poster_share
                poster_profile.save()

                if poster_share > 0:
                    RewardLedger.objects.create(
                        user=posted_by,
                        task=task,
                        amount=poster_share,
                        transaction_type='dispute_resolution',
                        description=f"Dispute resolution split refund for task: '{task.title}'"
                    )

                if taken_by and taker_share > 0:
                    taker_profile = taken_by.userprofile
                    taker_profile.rewards += taker_share
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=taken_by,
                        task=task,
                        amount=taker_share,
                        transaction_type='dispute_resolution',
                        description=f"Dispute resolution split award for task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()

            # Notifications
            Notification.objects.create(
                recipient=posted_by,
                message=f"Dispute for task '{task.title}' has been resolved: {dispute.get_resolution_outcome_display()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if taken_by:
                Notification.objects.create(
                    recipient=taken_by,
                    message=f"Dispute for task '{task.title}' has been resolved: {dispute.get_resolution_outcome_display()}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, f"Dispute resolved with outcome: {dispute.get_resolution_outcome_display()}.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        return HttpResponseForbidden("Permission denied. Only disputing parties can submit an appeal.")

    if dispute.status != 'resolved':
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.appeal_status not in [None, 'none', '']:
        messages.error(request, "An appeal has already been submitted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appeal_window_active:
        messages.error(request, "The appeal window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip() or request.POST.get('appeal_reason', '').strip()
    if not reason:
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.appeal_status = 'pending'
    dispute.appealed_by = request.user
    dispute.appeal_reason = reason
    dispute.save()

    other_party = task.taken_by if request.user == task.posted_by else task.posted_by
    if other_party:
        Notification.objects.create(
            recipient=other_party,
            message=f"An appeal was submitted for dispute on task '{task.title}' by {request.user.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Your appeal has been submitted successfully and is pending review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Permission denied. Only authorized staff members can review appeals.")

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.appeal_status != 'pending':
        messages.error(request, "This dispute does not have a pending appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('decision')
    if decision not in ['uphold', 'reverse', 'overturn']:
        messages.error(request, "Invalid appeal decision choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    posted_by = task.posted_by
    taken_by = task.taken_by

    with transaction.atomic():
        if decision == 'uphold':
            dispute.appeal_status = 'upheld'
            dispute.appeal_reviewed_by = request.user
            dispute.appeal_reviewed_at = timezone.now()
            dispute.save()
            messages.success(request, "Initial dispute resolution was upheld.")

        else:
            initial_outcome = dispute.resolution_outcome
            target_outcome = request.POST.get('new_outcome')
            if not target_outcome or target_outcome not in ['poster_favored', 'taker_favored', 'split']:
                if initial_outcome == 'poster_favored':
                    target_outcome = 'taker_favored'
                elif initial_outcome == 'taker_favored':
                    target_outcome = 'poster_favored'
                elif initial_outcome == 'split':
                    if dispute.appealed_by == posted_by:
                        target_outcome = 'poster_favored'
                    else:
                        target_outcome = 'taker_favored'
                else:
                    target_outcome = 'poster_favored'

            if initial_outcome == 'poster_favored':
                init_poster = task.reward
                init_taker = 0
            elif initial_outcome == 'taker_favored':
                init_poster = 0
                init_taker = task.reward
            elif initial_outcome == 'split':
                init_poster = task.reward // 2
                init_taker = task.reward - init_poster
            else:
                init_poster = 0
                init_taker = 0

            if target_outcome == 'poster_favored':
                target_poster = task.reward
                target_taker = 0
            elif target_outcome == 'taker_favored':
                target_poster = 0
                target_taker = task.reward
            elif target_outcome == 'split':
                target_poster = task.reward // 2
                target_taker = task.reward - target_poster

            delta_poster = target_poster - init_poster
            delta_taker = target_taker - init_taker

            if delta_poster != 0:
                p_prof = posted_by.userprofile
                p_prof.rewards = max(0, p_prof.rewards + delta_poster)
                p_prof.save()
                RewardLedger.objects.create(
                    user=posted_by,
                    task=task,
                    amount=delta_poster,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment for task: '{task.title}'"
                )

            if taken_by and delta_taker != 0:
                t_prof = taken_by.userprofile
                t_prof.rewards = max(0, t_prof.rewards + delta_taker)
                t_prof.save()
                RewardLedger.objects.create(
                    user=taken_by,
                    task=task,
                    amount=delta_taker,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment for task: '{task.title}'"
                )

            if target_outcome == 'poster_favored':
                task.status = 'cancelled'
            else:
                task.status = 'completed'
            task.save()

            dispute.resolution_outcome = target_outcome
            dispute.appeal_status = 'reversed'
            dispute.appeal_reviewed_by = request.user
            dispute.appeal_reviewed_at = timezone.now()
            dispute.save()

            messages.success(request, f"Appeal review complete. Decision reversed to: {dispute.get_resolution_outcome_display()}.")

        Notification.objects.create(
            recipient=posted_by,
            message=f"Appeal review for dispute on task '{task.title}' is complete: {dispute.get_appeal_status_display()}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if taken_by:
            Notification.objects.create(
                recipient=taken_by,
                message=f"Appeal review for dispute on task '{task.title}' is complete: {dispute.get_appeal_status_display()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return redirect('dispute_detail', dispute_id=dispute.id)
