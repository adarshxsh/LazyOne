import hashlib
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_party = (request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by)
    user_vote = None
    if request.user.is_authenticated:
        user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()

    revealed_votes = dispute.votes.filter(revealed=True)
    poster_votes_count = revealed_votes.filter(revealed_vote='poster').count()
    taker_votes_count = revealed_votes.filter(revealed_vote='taker').count()

    context = {
        'dispute': dispute,
        'task': task,
        'is_party': is_party,
        'can_vote': not is_party,
        'user_vote': user_vote,
        'total_commits_count': dispute.votes.count(),
        'revealed_votes_count': revealed_votes.count(),
        'poster_votes_count': poster_votes_count,
        'taker_votes_count': taker_votes_count,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def dispute_list_view(request):
    open_disputes = Dispute.objects.exclude(status='resolved').select_related('task', 'raised_by')
    resolved_disputes = Dispute.objects.filter(status='resolved').select_related('task', 'raised_by')[:10]
    context = {
        'open_disputes': open_disputes,
        'resolved_disputes': resolved_disputes,
    }
    return render(request, 'dispute_list.html', context)

@login_required(login_url='/login/')
@require_POST
def commit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by:
        messages.error(request, "Parties involved in the dispute cannot vote as jury.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status not in ('open', 'commit'):
        messages.error(request, "Commit phase is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already submitted a vote commitment for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commit_hash_input = request.POST.get('commit_hash', '').strip().lower()
    vote = request.POST.get('vote', '').strip().lower()
    salt = request.POST.get('salt', '').strip()

    if commit_hash_input:
        if len(commit_hash_input) != 64:
            messages.error(request, "Commit hash must be a 64-character SHA-256 hex string.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        final_hash = commit_hash_input
    elif vote and salt:
        if vote not in ('poster', 'taker'):
            messages.error(request, "Invalid vote choice. Must be 'poster' or 'taker'.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        final_hash = hashlib.sha256(f"{vote}:{salt}".encode('utf-8')).hexdigest().lower()
    else:
        messages.error(request, "You must provide either a vote choice and secret salt, or a valid commit hash.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        commit_hash=final_hash
    )

    Notification.objects.create(
        recipient=task.posted_by,
        message=f"A new jury vote commitment was submitted for dispute on task '{task.title}'.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    if task.taken_by and task.taken_by != task.posted_by:
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"A new jury vote commitment was submitted for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Your vote commitment has been securely recorded.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def reveal_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'reveal':
        messages.error(request, "Dispute is not in the reveal phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_obj = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
    if not vote_obj:
        messages.error(request, "You did not submit a vote commitment in the commit phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if vote_obj.revealed:
        messages.error(request, "You have already revealed your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote = request.POST.get('vote', '').strip().lower()
    salt = request.POST.get('salt', '').strip()

    if not vote or not salt:
        messages.error(request, "Both vote choice and secret salt are required to reveal your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if vote not in ('poster', 'taker'):
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    computed_hash = hashlib.sha256(f"{vote}:{salt}".encode('utf-8')).hexdigest().lower()
    if computed_hash != vote_obj.commit_hash.lower():
        messages.error(request, "Cryptographic hash mismatch! The provided vote choice and secret salt do not match your committed hash.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_obj.revealed = True
    vote_obj.revealed_vote = vote
    vote_obj.revealed_at = timezone.now()
    vote_obj.save()

    messages.success(request, "Your vote has been successfully verified and revealed!")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def advance_dispute_phase(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and request.user != dispute.raised_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to advance the phase of this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status in ('open', 'commit'):
        dispute.advance_to_reveal()
        messages.success(request, "Dispute phase successfully advanced to Reveal Phase.")
    elif dispute.status == 'reveal':
        dispute.tally_and_resolve()
        messages.success(request, "Dispute tallied and resolved successfully.")
    else:
        messages.error(request, "Dispute is already resolved.")

    return redirect('dispute_detail', dispute_id=dispute.id)

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
