from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, DisputeEvidence, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    
    if not is_participant and not request.user.is_staff and dispute.status != 'voting' and dispute.status != 'resolved':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_list = dispute.evidence.all().order_by('created_at')
    votes = dispute.votes.all()
    has_voted = votes.filter(voter=request.user).exists()
    poster_votes = votes.filter(voted_for=task.posted_by).count()
    taker_votes = votes.filter(voted_for=task.taken_by).count()

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_list': evidence_list,
        'votes': votes,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
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
def submit_evidence_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    evidence_text = request.POST.get('evidence_text') or request.POST.get('text')
    evidence_url = request.POST.get('evidence_url')
    if not evidence_text:
        messages.error(request, "Evidence text is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    try:
        dispute.submit_evidence(request.user, evidence_text, evidence_url)
        messages.success(request, "Evidence submitted successfully.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def start_voting_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    try:
        dispute.start_voting()
        messages.success(request, "Voting phase initiated.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_vote_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Selection required to vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    voted_for = get_object_or_404(User, id=voted_for_id)
    try:
        dispute.cast_vote(request.user, voted_for)
        messages.success(request, f"Vote registered for {voted_for.username}.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def finalize_resolution_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    outcome = request.POST.get('outcome')
    try:
        dispute.finalize_resolution(outcome=outcome if outcome else None)
        messages.success(request, "Dispute resolution finalized successfully.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    try:
        dispute.withdraw(request.user)
        messages.success(request, f"You have successfully withdrawn the dispute for '{dispute.task.title}'.")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, 'message') else str(e))
    return redirect('my_tasks')

