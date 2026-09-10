from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not request.user.is_superuser:
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
@require_POST
def resolve_dispute_admin(request, dispute_id):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Only authorized staff members can resolve disputes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    resolution_type = request.POST.get('resolution_type')
    resolution_notes = request.POST.get('resolution_notes')

    if resolution_type not in ['taker_win', 'poster_win', 'split']:
        messages.error(request, "Invalid resolution type selected.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if not resolution_notes:
        messages.error(request, "Resolution notes are required.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    with transaction.atomic():
        dispute.resolution_type = resolution_type
        dispute.resolution_notes = resolution_notes
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.status = 'resolved'
        dispute.save()

        if resolution_type == 'taker_win':
            task.status = 'completed'
            task.save()
            if taker:
                taker_profile = taker.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=taker,
                    task=task,
                    amount=task.reward,
                    transaction_type='arbitration_award',
                    description=f"Arbitration award for task: '{task.title}'"
                )
        elif resolution_type == 'poster_win':
            task.status = 'cancelled'
            task.save()
            poster_profile = poster.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='arbitration_refund',
                description=f"Arbitration refund for task: '{task.title}'"
            )
        elif resolution_type == 'split':
            task.status = 'completed'
            task.save()
            taker_share = task.reward // 2
            poster_share = task.reward - taker_share

            if taker and taker_share > 0:
                taker_profile = taker.userprofile
                taker_profile.rewards += taker_share
                taker_profile.save()
                RewardLedger.objects.create(
                    user=taker,
                    task=task,
                    amount=taker_share,
                    transaction_type='arbitration_award',
                    description=f"Arbitration split award for task: '{task.title}'"
                )

            if poster_share > 0:
                poster_profile = poster.userprofile
                poster_profile.rewards += poster_share
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=poster_share,
                    transaction_type='arbitration_refund',
                    description=f"Arbitration split refund for task: '{task.title}'"
                )

        Notification.objects.create(
            recipient=poster,
            message=f"Dispute for task '{task.title}' has been resolved by staff.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if taker:
            Notification.objects.create(
                recipient=taker,
                message=f"Dispute for task '{task.title}' has been resolved by staff.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Dispute resolved successfully with decision: {dispute.get_resolution_type_display()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if dispute.status != 'resolved':
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if dispute.appeal_status is not None:
        messages.error(request, "An appeal has already been submitted for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if not dispute.resolved_at or timezone.now() > dispute.resolved_at + timedelta(days=7):
        messages.error(request, "The 7-day appeal window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    appeal_reason = request.POST.get('appeal_reason') or request.POST.get('reason')
    if not appeal_reason:
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    with transaction.atomic():
        dispute.appeal_status = 'pending'
        dispute.appealed_by = request.user
        dispute.appeal_reason = appeal_reason
        dispute.appealed_at = timezone.now()
        dispute.save()

        other_user = task.taken_by if request.user == task.posted_by else task.posted_by
        if other_user:
            Notification.objects.create(
                recipient=other_user,
                message=f"{request.user.username} has submitted an appeal for dispute on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, "Appeal submitted successfully and is now pending review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def resolve_appeal_admin(request, dispute_id):
    if not (request.user.is_superuser or request.user.is_staff):
        messages.error(request, "Only senior staff members can resolve appeals.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.appeal_status != 'pending':
        messages.error(request, "This appeal is not pending decision.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    appeal_decision = request.POST.get('appeal_decision')
    appeal_notes = request.POST.get('appeal_notes')

    if appeal_decision not in ['uphold', 'overturn']:
        messages.error(request, "Invalid appeal decision selected.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    if not appeal_notes:
        messages.error(request, "Appeal notes are required.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    task = dispute.task
    poster = task.posted_by
    taker = task.taken_by

    with transaction.atomic():
        dispute.appeal_resolved_by = request.user
        dispute.appeal_notes = appeal_notes

        if appeal_decision == 'uphold':
            dispute.appeal_status = 'upheld'
            dispute.save()
        elif appeal_decision == 'overturn':
            dispute.appeal_status = 'overturned'
            dispute.save()

            initial_res = dispute.resolution_type
            if initial_res == 'taker_win':
                if taker:
                    taker_profile = taker.userprofile
                    taker_profile.rewards -= task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=taker,
                        task=task,
                        amount=-task.reward,
                        transaction_type='appeal_adjustment',
                        description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                    )
                poster_profile = poster.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=task.reward,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                )
                task.status = 'cancelled'
                task.save()

            elif initial_res == 'poster_win':
                poster_profile = poster.userprofile
                poster_profile.rewards -= task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=-task.reward,
                    transaction_type='appeal_adjustment',
                    description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                )
                if taker:
                    taker_profile = taker.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=taker,
                        task=task,
                        amount=task.reward,
                        transaction_type='appeal_adjustment',
                        description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                    )
                task.status = 'completed'
                task.save()

            elif initial_res == 'split':
                taker_share = task.reward // 2
                poster_share = task.reward - taker_share

                if dispute.appealed_by == poster:
                    if taker and taker_share > 0:
                        taker_profile = taker.userprofile
                        taker_profile.rewards -= taker_share
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=taker,
                            task=task,
                            amount=-taker_share,
                            transaction_type='appeal_adjustment',
                            description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                        )
                    if taker_share > 0:
                        poster_profile = poster.userprofile
                        poster_profile.rewards += taker_share
                        poster_profile.save()
                        RewardLedger.objects.create(
                            user=poster,
                            task=task,
                            amount=taker_share,
                            transaction_type='appeal_adjustment',
                            description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                        )
                    task.status = 'cancelled'
                    task.save()
                else:
                    if poster_share > 0:
                        poster_profile = poster.userprofile
                        poster_profile.rewards -= poster_share
                        poster_profile.save()
                        RewardLedger.objects.create(
                            user=poster,
                            task=task,
                            amount=-poster_share,
                            transaction_type='appeal_adjustment',
                            description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                        )
                    if taker and poster_share > 0:
                        taker_profile = taker.userprofile
                        taker_profile.rewards += poster_share
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=taker,
                            task=task,
                            amount=poster_share,
                            transaction_type='appeal_adjustment',
                            description=f"Appeal adjustment (overturned) for task: '{task.title}'"
                        )
                    task.status = 'completed'
                    task.save()

        Notification.objects.create(
            recipient=poster,
            message=f"Appeal decision for dispute '{task.title}' has been finalized: {dispute.get_appeal_status_display()}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if taker:
            Notification.objects.create(
                recipient=taker,
                message=f"Appeal decision for dispute '{task.title}' has been finalized: {dispute.get_appeal_status_display()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appeal adjudicated successfully: decision is {dispute.get_appeal_status_display()}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

