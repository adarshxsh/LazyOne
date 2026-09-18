from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = hasattr(request.user, 'userprofile') and request.user.userprofile.is_juror
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    is_arbitrator = request.user.is_staff or is_juror
    context = {
        'dispute': dispute,
        'task': task,
        'is_arbitrator': is_arbitrator,
        'can_be_appealed': dispute.can_be_appealed,
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
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_juror = hasattr(request.user, 'userprofile') and request.user.userprofile.is_juror
    if not (request.user.is_staff or is_juror):
        messages.error(request, "Only authorized staff or community jurors can resolve disputes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status not in ['open', 'appealed']:
        messages.error(request, "This dispute cannot be resolved in its current state.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_param = request.POST.get('winner') or request.POST.get('winner_id')
    resolution_reason = request.POST.get('resolution_reason', '').strip() or request.POST.get('reason', '').strip()

    if not winner_param:
        messages.error(request, "A winning party must be selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner_user = None
    if str(winner_param).lower() in ['posted_by', 'poster']:
        winner_user = task.posted_by
    elif str(winner_param).lower() in ['taken_by', 'taker']:
        winner_user = task.taken_by
    else:
        try:
            winner_user = User.objects.get(id=int(winner_param))
        except (ValueError, TypeError, User.DoesNotExist):
            winner_user = None

    if not winner_user or (winner_user != task.posted_by and winner_user != task.taken_by):
        messages.error(request, "Invalid winning party selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute = Dispute.objects.select_for_update().get(id=dispute_id)
        task = dispute.task

        previous_winner = dispute.winner if dispute.status == 'appealed' else None

        if dispute.status == 'open':
            winner_profile = winner_user.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()

            RewardLedger.objects.create(
                user=winner_user,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Reward award for resolved dispute on task: '{task.title}'"
            )
        elif dispute.status == 'appealed':
            if previous_winner and previous_winner != winner_user:
                prev_profile = previous_winner.userprofile
                prev_profile.rewards -= task.reward
                prev_profile.save()
                RewardLedger.objects.create(
                    user=previous_winner,
                    task=task,
                    amount=-task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Reversal of award following appeal for task: '{task.title}'"
                )

                winner_profile = winner_user.userprofile
                winner_profile.rewards += task.reward
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner_user,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Reward award following appeal for task: '{task.title}'"
                )

        if winner_user == task.taken_by:
            task.status = 'completed'
        else:
            task.status = 'cancelled'
        task.save()

        dispute.status = 'resolved'
        dispute.winner = winner_user
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        if resolution_reason:
            dispute.resolution_reason = resolution_reason
        dispute.save()

        detail_url = reverse('dispute_detail', args=[dispute.id])
        notification_msg = f"Dispute for task '{task.title}' has been resolved in favor of {winner_user.username}."
        
        Notification.objects.create(
            recipient=task.posted_by,
            message=notification_msg,
            link=detail_url
        )
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=notification_msg,
                link=detail_url
            )

    messages.success(request, f"Dispute resolved in favor of {winner_user.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to appeal this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'resolved':
        if dispute.status == 'appealed':
            messages.error(request, "This dispute has already been appealed.")
        else:
            messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.can_be_appealed:
        messages.error(request, "The 48-hour window to appeal this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_reason = request.POST.get('reason', '').strip() or request.POST.get('appeal_reason', '').strip()
    if not appeal_reason:
        messages.error(request, "A reason is required to submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.status = 'appealed'
        dispute.appealed_by = request.user
        dispute.appealed_at = timezone.now()
        dispute.appeal_reason = appeal_reason
        dispute.save()

        detail_url = reverse('dispute_detail', args=[dispute.id])
        notification_msg = f"{request.user.username} has filed an appeal for the dispute on task '{task.title}'."

        Notification.objects.create(
            recipient=task.posted_by,
            message=notification_msg,
            link=detail_url
        )
        if task.taken_by and task.taken_by != task.posted_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=notification_msg,
                link=detail_url
            )

    messages.success(request, "Appeal submitted successfully. The dispute is now under review.")
    return redirect('dispute_detail', dispute_id=dispute.id)
