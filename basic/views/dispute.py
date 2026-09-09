from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST
from datetime import timedelta

from ..models import Dispute, DisputeAppeal, Task, Notification, RewardLedger, Conversation

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    # Get conversation messages if available
    chat_messages = []
    conversation = Conversation.objects.filter(task=task).first()
    if conversation:
        chat_messages = conversation.messages.all().order_by('timestamp')

    # Check appeal eligibility for current user
    user_appeals = dispute.appeals.filter(appellant=request.user)
    has_appealed = user_appeals.exists()
    can_appeal = (
        dispute.status == 'resolved' and
        (request.user == task.posted_by or request.user == task.taken_by) and
        dispute.is_appealable() and
        not has_appealed
    )

    context = {
        'dispute': dispute,
        'task': task,
        'chat_messages': chat_messages,
        'can_appeal': can_appeal,
        'has_appealed': has_appealed,
        'appeals': dispute.appeals.all().order_by('-created_at'),
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
def staff_dispute_dashboard(request):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can access the arbitration dashboard.")
        return redirect('home')

    open_disputes = Dispute.objects.filter(status='open').order_by('-created_at')
    resolved_disputes = Dispute.objects.filter(status='resolved').order_by('-resolved_at')
    pending_appeals = DisputeAppeal.objects.filter(status='pending').order_by('-created_at')
    processed_appeals = DisputeAppeal.objects.exclude(status='pending').order_by('-reviewed_at')

    context = {
        'open_disputes': open_disputes,
        'resolved_disputes': resolved_disputes,
        'pending_appeals': pending_appeals,
        'processed_appeals': processed_appeals,
    }
    return render(request, 'staff_dispute_dashboard.html', context)

@login_required(login_url='/login/')
@require_POST
def arbitrate_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can arbitrate disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task
    resolution = request.POST.get('resolution')
    notes = request.POST.get('notes', '').strip()

    if resolution not in ['refund_poster', 'pay_taker', 'cancel']:
        messages.error(request, "Invalid resolution option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        now = timezone.now()
        dispute.status = 'resolved'
        dispute.resolution_outcome = resolution
        dispute.resolved_by = request.user
        dispute.resolved_at = now
        dispute.resolution_notes = notes
        dispute.save()

        if resolution in ['refund_poster', 'cancel']:
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_arbitration',
                description=f"Arbitration refund for task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()
            outcome_text = "Refund issued to poster"

        elif resolution == 'pay_taker':
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_arbitration',
                    description=f"Arbitration payment awarded for task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()
            outcome_text = "Payment awarded to taker"

        # Automated notifications to both parties
        link = reverse('dispute_detail', args=[dispute.id])
        notif_msg = f"Dispute for task '{task.title}' has been arbitrated by staff ({outcome_text})."

        Notification.objects.create(recipient=task.posted_by, message=notif_msg, link=link)
        if task.taken_by:
            Notification.objects.create(recipient=task.taken_by, message=notif_msg, link=link)

    messages.success(request, f"Dispute for task '{task.title}' arbitrated successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='resolved')
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to appeal this dispute.")
        return redirect('home')

    if not dispute.is_appealable():
        messages.error(request, "The 7-day appeal window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeAppeal.objects.filter(dispute=dispute, appellant=request.user).exists():
        messages.error(request, "You have already submitted an appeal for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, "An appeal reason is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal = DisputeAppeal.objects.create(
        dispute=dispute,
        appellant=request.user,
        reason=reason,
        status='pending'
    )

    # Notify opponent
    opponent = task.taken_by if request.user == task.posted_by else task.posted_by
    if opponent:
        Notification.objects.create(
            recipient=opponent,
            message=f"{request.user.username} has submitted a formal appeal regarding dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Your administrative appeal has been submitted for senior review.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, appeal_id):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can review appeals.")
        return redirect('home')

    appeal = get_object_or_404(DisputeAppeal, id=appeal_id, status='pending')
    dispute = appeal.dispute
    task = dispute.task

    action = request.POST.get('action') # 'uphold' or 'overturn'
    notes = request.POST.get('notes', '').strip()

    if action not in ['uphold', 'overturn']:
        messages.error(request, "Invalid appeal review action.")
        return redirect('staff_dispute_dashboard')

    with transaction.atomic():
        now = timezone.now()
        appeal.reviewed_by = request.user
        appeal.reviewed_at = now
        appeal.review_notes = notes

        link = reverse('dispute_detail', args=[dispute.id])

        if action == 'uphold':
            appeal.status = 'upheld'
            appeal.save()

            msg = f"Appeal for task '{task.title}' was reviewed and initial ruling was upheld."
            Notification.objects.create(recipient=task.posted_by, message=msg, link=link)
            if task.taken_by:
                Notification.objects.create(recipient=task.taken_by, message=msg, link=link)

            messages.success(request, f"Appeal for task '{task.title}' upheld successfully.")

        elif action == 'overturn':
            # Determine new resolution
            prev_resolution = dispute.resolution_outcome
            new_resolution = request.POST.get('new_resolution')
            if not new_resolution:
                new_resolution = 'pay_taker' if prev_resolution in ['refund_poster', 'cancel'] else 'refund_poster'

            appeal.status = 'overturned'
            appeal.new_resolution = new_resolution
            appeal.save()

            # Execute point reversal and re-allocation
            reward_amount = task.reward

            if prev_resolution in ['refund_poster', 'cancel'] and new_resolution == 'pay_taker':
                # Reverse poster refund, give points to taker
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards -= reward_amount
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=-reward_amount,
                    transaction_type='appeal_reversal',
                    description=f"Appeal overturn: Reversal of refund for task '{task.title}'"
                )

                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += reward_amount
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=reward_amount,
                        transaction_type='dispute_arbitration',
                        description=f"Appeal overturn: Payment awarded for task '{task.title}'"
                    )

                task.status = 'completed'
                task.save()

            elif prev_resolution == 'pay_taker' and new_resolution in ['refund_poster', 'cancel']:
                # Reverse taker payment, give points back to poster
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards -= reward_amount
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=-reward_amount,
                        transaction_type='appeal_reversal',
                        description=f"Appeal overturn: Reversal of payment for task '{task.title}'"
                    )

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += reward_amount
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=reward_amount,
                    transaction_type='dispute_arbitration',
                    description=f"Appeal overturn: Refund awarded for task '{task.title}'"
                )

                task.status = 'cancelled'
                task.save()

            dispute.resolution_outcome = new_resolution
            dispute.save()

            msg = f"Appeal for task '{task.title}' was overturned. New ruling: {dispute.get_resolution_outcome_display()}."
            Notification.objects.create(recipient=task.posted_by, message=msg, link=link)
            if task.taken_by:
                Notification.objects.create(recipient=task.taken_by, message=msg, link=link)

            messages.success(request, f"Appeal for task '{task.title}' overturned successfully.")

    return redirect('staff_dispute_dashboard')
