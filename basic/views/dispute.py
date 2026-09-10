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

    if dispute.status != 'open' and not is_participant and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'is_participant': is_participant,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "You cannot vote on your own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    verdict = request.POST.get('verdict')
    if verdict not in ['poster', 'taker']:
        messages.error(request, "Invalid vote verdict selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        with transaction.atomic():
            dispute = Dispute.objects.select_for_update().get(id=dispute_id)
            if dispute.status != 'open':
                messages.error(request, "This dispute is no longer open for voting.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
                messages.error(request, "You have already voted on this dispute.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            DisputeVote.objects.create(
                dispute=dispute,
                voter=request.user,
                verdict=verdict
            )

            poster_votes = DisputeVote.objects.filter(dispute=dispute, verdict='poster').count()
            taker_votes = DisputeVote.objects.filter(dispute=dispute, verdict='taker').count()

            dispute.poster_votes = poster_votes
            dispute.taker_votes = taker_votes

            total_votes = poster_votes + taker_votes

            if total_votes >= dispute.required_votes:
                if poster_votes > taker_votes:
                    dispute.status = 'resolved'
                    dispute.resolution_verdict = 'poster'
                    dispute.save()

                    task.status = 'cancelled'
                    task.save()

                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()

                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refund for cancelled task: '{task.title}'"
                    )

                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' was resolved in your favor.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Dispute for task '{task.title}' was resolved in favor of the poster.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                elif taker_votes > poster_votes:
                    dispute.status = 'resolved'
                    dispute.resolution_verdict = 'taker'
                    dispute.save()

                    task.status = 'completed'
                    task.save()

                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Completed task: '{task.title}'"
                    )

                    Notification.objects.create(
                        recipient=task.posted_by,
                        message=f"Dispute for task '{task.title}' was resolved in favor of the taker.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Dispute for task '{task.title}' was resolved in your favor.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
                else:
                    dispute.save()
            else:
                dispute.save()

            messages.success(request, "Your vote has been cast successfully.")
    except IntegrityError:
        messages.error(request, "You have already voted on this dispute.")

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
