from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, DisputeVote, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from ..services import select_and_lock_jurors, resolve_dispute

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_counterparty = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = dispute.jurors.filter(id=request.user.id).exists()

    if not (is_counterparty or is_juror or request.user.is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = dispute.votes.filter(voter=request.user).first()
    has_voted = user_vote is not None

    context = {
        'dispute': dispute,
        'task': task,
        'is_counterparty': is_counterparty,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'jurors': dispute.jurors.all(),
        'votes': dispute.votes.all(),
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

        try:
            with transaction.atomic():
                dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
                success = select_and_lock_jurors(dispute, panel_size=3, stake_amount=100)
                if not success:
                    raise ValueError("Unable to form a neutral juror panel of 3 qualified users with required stake (minimum 100 points).")
                task.status = 'disputed'
                task.save()
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            messages.success(request, "Dispute raised successfully. A panel of neutral jurors has been assigned.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('my_tasks')

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def cast_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.jurors.filter(id=request.user.id).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a candidate to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        voted_for_id = int(voted_for_id)
    except (ValueError, TypeError):
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if voted_for_id not in [task.posted_by.id, task.taken_by.id if task.taken_by else None]:
        messages.error(request, "You must vote for one of the task counterparties.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = task.posted_by if voted_for_id == task.posted_by.id else task.taken_by

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        voted_for=voted_for_user
    )
    messages.success(request, "Your vote has been cast successfully.")

    if dispute.votes.count() >= dispute.jurors.count():
        resolve_dispute(dispute)
        messages.info(request, "All jurors have voted. The dispute has been resolved.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user, status='open')
    task = dispute.task
    with transaction.atomic():
        for juror in dispute.jurors.all():
            juror_profile = juror.userprofile
            juror_profile.rewards += 100
            juror_profile.save()
            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=100,
                transaction_type='juror_stake_return',
                description=f"Returned 100 points stake due to dispute withdrawal on task '{task.title}'"
            )
            Notification.objects.create(
                recipient=juror,
                message=f"The dispute for task '{task.title}' was withdrawn. Your 100 points stake has been returned.",
                link=reverse('my_tasks')
            )
        task.status = 'in_progress'
        task.save()
        dispute.delete()
        other_user = task.posted_by if request.user == task.taken_by else task.taken_by
        if other_user:
            Notification.objects.create(
                recipient=other_user,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
        messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Locked stakes were returned to jurors.")
    return redirect('my_tasks')
