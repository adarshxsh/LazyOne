from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    if not is_participant and not request.user.is_staff and dispute.status != 'open':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    tally = dispute.get_weighted_tally()
    w_poster = tally['poster_weight']
    w_taker = tally['taker_weight']
    w_total = tally['total_weight']

    poster_pct = round((w_poster / w_total * 100), 1) if w_total > 0 else 0
    taker_pct = round((w_taker / w_total * 100), 1) if w_total > 0 else 0
    quorum_pct = min(100, round((w_total / 10 * 100), 1))
    voter_quorum_pct = min(100, round((tally['total_voters'] / 3 * 100), 1))

    has_quorum = dispute.has_quorum()
    can_vote = dispute.can_user_vote(request.user)
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first() if request.user.is_authenticated else None
    user_weight = dispute.get_vote_weight(request.user) if request.user.is_authenticated else 0

    conversation = getattr(task, 'conversation', None) or task.main_chat
    chat_messages = conversation.messages.all() if conversation else None

    context = {
        'dispute': dispute,
        'task': task,
        'tally': tally,
        'poster_pct': poster_pct,
        'taker_pct': taker_pct,
        'quorum_pct': quorum_pct,
        'voter_quorum_pct': voter_quorum_pct,
        'has_quorum': has_quorum,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'user_weight': user_weight,
        'conversation': conversation,
        'chat_messages': chat_messages,
        'is_participant': is_participant,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    with transaction.atomic():
        dispute = Dispute.objects.select_for_update().get(id=dispute_id)
        if dispute.status != 'open':
            messages.error(request, "This dispute is no longer open for voting.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        if not dispute.can_user_vote(request.user):
            messages.error(request, "You are not eligible to vote on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        vote_choice = request.POST.get('vote_choice')
        if vote_choice not in ['poster', 'taker']:
            messages.error(request, "Invalid vote choice.")
            return redirect('dispute_detail', dispute_id=dispute.id)

        weight = dispute.get_vote_weight(request.user)
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote_choice=vote_choice,
            weight=weight
        )

        resolved = dispute.check_and_execute_consensus()
        if resolved:
            messages.success(request, "Your vote has been recorded. Voting quorum and supermajority consensus were reached, and the dispute has been resolved!")
        else:
            messages.success(request, f"Your vote (weight: {weight}) has been recorded successfully.")

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
