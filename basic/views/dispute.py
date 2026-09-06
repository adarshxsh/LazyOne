from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, DisputeVote, RewardLedger
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction

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

    if request.user.is_staff:
        total_votes = dispute.votes.count()
        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
        context.update({
            'show_vote_tallies': True,
            'total_votes': total_votes,
            'poster_votes': poster_votes,
            'taker_votes': taker_votes,
        })

    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    redirect_url = reverse('chat_view', args=[task.conversation.id]) if hasattr(task, 'conversation') and task.conversation else reverse('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect(redirect_url)

    if request.user == task.posted_by or (task.taken_by and request.user == task.taken_by):
        messages.error(request, "Parties involved in the task dispute cannot vote.")
        return redirect(redirect_url)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect(redirect_url)

    voted_for_id = request.POST.get('voted_for_id')
    try:
        voted_for_id = int(voted_for_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid vote option.")
        return redirect(redirect_url)

    valid_candidates = [task.posted_by.id]
    if task.taken_by:
        valid_candidates.append(task.taken_by.id)

    if voted_for_id not in valid_candidates:
        messages.error(request, "Invalid choice for dispute vote.")
        return redirect(redirect_url)

    voted_for_user = get_object_or_404(User, id=voted_for_id)
    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        voted_for=voted_for_user
    )

    messages.success(request, "Your advisory vote has been recorded.")
    return redirect(redirect_url)

@login_required(login_url='/login/')
@require_POST
def settle_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only staff members can settle disputes.")
        return redirect('home')

    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner = request.POST.get('winner')  # 'poster' or 'taker'

    with transaction.atomic():
        if winner == 'poster':
            # Refund escrow reward to poster
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Staff dispute settlement refund for task: '{task.title}'"
            )

            task.status = 'cancelled'
            task.save()
            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff resolved the dispute for '{task.title}' in your favor. Points refunded.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff resolved the dispute for '{task.title}' in favor of the poster.",
                    link=reverse('my_tasks')
                )

            messages.success(request, f"Dispute settled in favor of poster {task.posted_by.username}. {task.reward} points refunded.")

        elif winner == 'taker' and task.taken_by:
            # Award reward to taker
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Staff dispute settlement payout for task: '{task.title}'"
            )

            task.status = 'completed'
            task.save()
            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Staff resolved the dispute for '{task.title}' in your favor. {task.reward} points awarded.",
                link=reverse('my_tasks')
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff resolved the dispute for '{task.title}' in favor of {task.taken_by.username}.",
                link=reverse('my_tasks')
            )

            messages.success(request, f"Dispute settled in favor of taker {task.taken_by.username}. {task.reward} points awarded.")

        else:
            messages.error(request, "Invalid settlement selection.")

    return redirect('dispute_detail', dispute_id=dispute.id)

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
