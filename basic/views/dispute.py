from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
import random

from ..models import Dispute, Task, Notification, JuryAssignment, DisputeVote, RewardLedger

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).exists()
    
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    is_expired = timezone.now() > (dispute.created_at + timedelta(hours=48))
    
    has_voted = False
    user_vote = None
    if is_juror:
        user_vote_obj = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()
        if user_vote_obj:
            has_voted = True
            user_vote = user_vote_obj

    votes = dispute.votes.all()
    poster_votes = votes.filter(vote='poster_wins').count()
    taker_votes = votes.filter(vote='taker_wins').count()
    
    total_assignments = dispute.assignments.count()
    quorum_needed = (total_assignments // 2) + 1 if total_assignments > 0 else 3

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'is_expired': is_expired,
        'votes': votes,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_assignments': total_assignments,
        'quorum_needed': quorum_needed,
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
        
        with transaction.atomic():
            dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
            task.status = 'disputed'
            task.save()
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

            # Sample randomized pool of eligible jurors excluding poster and taker
            excluded_ids = [task.posted_by.id]
            if task.taken_by:
                excluded_ids.append(task.taken_by.id)
            
            eligible_users = list(User.objects.exclude(id__in=excluded_ids))
            selected_jurors = random.sample(eligible_users, min(len(eligible_users), 5))

            for juror in selected_jurors:
                JuryAssignment.objects.create(dispute=dispute, juror=juror)
                Notification.objects.create(
                    recipient=juror,
                    message=f"You have been selected as a peer juror for a dispute on task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, "Dispute raised successfully. Peer jury has been assigned.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if timezone.now() > (dispute.created_at + timedelta(hours=48)):
        messages.error(request, "The 48-hour voting window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).first()
    if not assignment:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    rationale = request.POST.get('rationale', '').strip()

    if vote_choice not in ['poster_wins', 'taker_wins']:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not rationale:
        messages.error(request, "A written rationale is required to submit your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote=vote_choice,
            rationale=rationale
        )
        assignment.status = 'voted'
        assignment.save()

        total_assigned = dispute.assignments.count()
        quorum_needed = (total_assigned // 2) + 1 if total_assigned > 0 else 3

        poster_votes = dispute.votes.filter(vote='poster_wins').count()
        taker_votes = dispute.votes.filter(vote='taker_wins').count()

        task = dispute.task

        if poster_votes >= quorum_needed:
            dispute.status = 'resolved'
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
                description=f"Dispute resolved in your favor: Refund for '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' resolved in your favor by peer jury.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for '{task.title}' resolved in favor of the task poster.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, "Vote submitted. Jury consensus reached: Poster wins! Points refunded.")

        elif taker_votes >= quorum_needed:
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'completed'
            task.save()

            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Dispute resolved in your favor: Reward for '{task.title}'"
                )

                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for '{task.title}' resolved in your favor by peer jury. {task.reward} points awarded!",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for '{task.title}' resolved in favor of the task taker.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

            messages.success(request, "Vote submitted. Jury consensus reached: Taker wins! Points awarded.")
        else:
            messages.success(request, "Your vote has been submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)

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
