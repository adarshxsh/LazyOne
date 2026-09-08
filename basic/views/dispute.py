from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeAppeal, Task, Notification, RewardLedger


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    is_participant = request.user in (task.posted_by, task.taken_by)
    user_appealed = False
    can_appeal = False
    within_window = False

    if dispute.status == 'resolved' and dispute.resolved_at:
        window_duration = timedelta(hours=48)
        within_window = (timezone.now() - dispute.resolved_at) <= window_duration

    if is_participant:
        user_appealed = DisputeAppeal.objects.filter(dispute=dispute, appellant=request.user).exists()
        can_appeal = (dispute.status == 'resolved') and within_window and (not user_appealed)

    context = {
        'dispute': dispute,
        'task': task,
        'can_arbitrate': request.user.is_staff and dispute.status == 'open',
        'can_appeal': can_appeal,
        'user_appealed': user_appealed,
        'within_window': within_window,
        'can_review_appeal': request.user.is_staff and dispute.status == 'appealed',
        'appeals': dispute.appeals.all().order_by('created_at'),
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
    if dispute.status != 'open':
        messages.error(request, "Cannot withdraw a dispute that is no longer open.")
        return redirect('dispute_detail', dispute_id=dispute.id)
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
def staff_disputes_list(request):
    if not request.user.is_staff:
        messages.error(request, "Access restricted to staff members.")
        return redirect('home')

    open_disputes = Dispute.objects.filter(status='open').order_by('-created_at')
    appealed_disputes = Dispute.objects.filter(status='appealed').order_by('-created_at')
    resolved_disputes = Dispute.objects.filter(status__in=['resolved', 'finalized']).order_by('-created_at')[:20]

    context = {
        'open_disputes': open_disputes,
        'appealed_disputes': appealed_disputes,
        'resolved_disputes': resolved_disputes,
    }
    return render(request, 'staff_disputes.html', context)


@login_required(login_url='/login/')
@require_POST
def arbitrate_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can arbitrate disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for arbitration.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_choice = request.POST.get('winner')
    note = request.POST.get('note', '').strip()
    task = dispute.task

    if winner_choice == 'poster':
        winner_user = task.posted_by
    elif winner_choice == 'taker':
        winner_user = task.taken_by
    else:
        messages.error(request, "Invalid winner selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not winner_user:
        messages.error(request, "Task does not have a valid winner participant.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        winner_profile = winner_user.userprofile
        winner_profile.rewards += task.reward
        winner_profile.save()

        RewardLedger.objects.create(
            user=winner_user,
            task=task,
            amount=task.reward,
            transaction_type='dispute_resolution',
            description=f"Arbitration reward for task: '{task.title}'"
        )

        dispute.status = 'resolved'
        dispute.winner = winner_user
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.resolution_note = note
        dispute.save()

        task.status = 'resolved'
        task.save()

        detail_url = reverse('dispute_detail', args=[dispute.id])
        notif_msg = f"Staff has resolved the dispute for '{task.title}'. Winner: {winner_user.username}."
        Notification.objects.create(recipient=task.posted_by, message=notif_msg, link=detail_url)
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(recipient=task.taken_by, message=notif_msg, link=detail_url)

    messages.success(request, f"Dispute resolved. Reward points assigned to {winner_user.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task participants can appeal this dispute.")
        return redirect('home')

    if dispute.status != 'resolved':
        messages.error(request, "An appeal can only be submitted for a resolved dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.resolved_at and (timezone.now() - dispute.resolved_at > timedelta(hours=48)):
        messages.error(request, "The appeal window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeAppeal.objects.filter(dispute=dispute, appellant=request.user).exists():
        messages.error(request, "You have already submitted an appeal for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, "A reason is required to submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            reason=reason
        )
        dispute.status = 'appealed'
        dispute.save()

        detail_url = reverse('dispute_detail', args=[dispute.id])
        notif_msg = f"{request.user.username} has lodged an appeal for the dispute on '{task.title}'."
        Notification.objects.create(recipient=task.posted_by, message=notif_msg, link=detail_url)
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(recipient=task.taken_by, message=notif_msg, link=detail_url)

    messages.success(request, "Your appeal request has been lodged successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can review dispute appeals.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'appealed':
        messages.error(request, "This dispute is not currently under appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    verdict = request.POST.get('verdict')
    note = request.POST.get('note', '').strip()
    task = dispute.task

    if verdict not in ('uphold', 'reverse'):
        messages.error(request, "Invalid verdict selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if verdict == 'reverse':
            old_winner = dispute.winner
            new_winner = task.taken_by if old_winner == task.posted_by else task.posted_by

            if old_winner:
                old_profile = old_winner.userprofile
                old_profile.rewards -= task.reward
                old_profile.save()
                RewardLedger.objects.create(
                    user=old_winner,
                    task=task,
                    amount=-task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Appeal reversal deduction for task: '{task.title}'"
                )

            new_profile = new_winner.userprofile
            new_profile.rewards += task.reward
            new_profile.save()
            RewardLedger.objects.create(
                user=new_winner,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Appeal reversal reward for task: '{task.title}'"
            )

            dispute.winner = new_winner

        dispute.status = 'finalized'
        dispute.final_reviewer = request.user
        dispute.final_verdict = verdict
        dispute.final_verdict_note = note
        dispute.finalized_at = timezone.now()
        dispute.save()

        detail_url = reverse('dispute_detail', args=[dispute.id])
        notif_msg = f"Final appeal verdict issued for dispute on '{task.title}': {verdict.title()}."
        Notification.objects.create(recipient=task.posted_by, message=notif_msg, link=detail_url)
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(recipient=task.taken_by, message=notif_msg, link=detail_url)

    messages.success(request, f"Appeal review complete. Verdict '{verdict.title()}' logged and dispute finalized.")
    return redirect('dispute_detail', dispute_id=dispute.id)
