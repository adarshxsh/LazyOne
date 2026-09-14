from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, DisputeVote, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction, IntegrityError

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    if not is_participant and not request.user.is_staff and task.status != 'disputed':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()
    total_votes = dispute.votes.count()

    user_vote = dispute.votes.filter(voter=request.user).first() if request.user.is_authenticated else None
    can_vote = (
        request.user.is_authenticated
        and task.status == 'disputed'
        and dispute.status == 'open'
        and not is_participant
        and user_vote is None
    )

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'is_participant': is_participant,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open' or task.status != 'disputed':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        try:
            DisputeVote.objects.create(
                dispute=dispute,
                voter=request.user,
                choice=choice
            )
        except IntegrityError:
            messages.error(request, "You have already voted on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        messages.success(request, "Your vote has been submitted successfully.")

        total_votes = dispute.votes.count()
        if total_votes >= 5:
            poster_votes = dispute.votes.filter(choice='poster').count()
            taker_votes = dispute.votes.filter(choice='taker').count()

            if taker_votes > poster_votes:
                task.status = 'completed'
                task.save()
                dispute.status = 'resolved'
                dispute.save()

                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Dispute resolved in your favor for task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor by community vote ({taker_votes}-{poster_votes}). {task.reward} points awarded.",
                    link=reverse('my_tasks')
                )
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of taker by community vote ({taker_votes}-{poster_votes}).",
                    link=reverse('my_tasks')
                )
            else:
                task.status = 'cancelled'
                task.save()
                dispute.status = 'resolved'
                dispute.save()

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Dispute resolved with refund for task: '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor by community vote ({poster_votes}-{taker_votes}). {task.reward} points refunded.",
                    link=reverse('my_tasks')
                )
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of poster by community vote ({poster_votes}-{taker_votes}).",
                    link=reverse('my_tasks')
                )

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
