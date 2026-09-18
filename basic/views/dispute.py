from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, DisputeVote, Task, Notification, RewardLedger

JURY_QUORUM = 3

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_party = (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff)
    is_jury_viewable = dispute.status in ['jury_voting', 'resolved', 'closed', 'under_review']
    
    if not is_party and not is_jury_viewable:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    can_vote = dispute.can_user_vote(request.user)
    user_vote = dispute.votes.filter(juror=request.user).first() if request.user.is_authenticated else None

    context = {
        'dispute': dispute,
        'task': task,
        'can_vote': can_vote,
        'user_vote': user_vote,
        'quorum': JURY_QUORUM,
        'is_party': is_party,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You can only raise a dispute for a task you posted or took.")
        return redirect('my_tasks')

    if task.status != 'in_progress':
        messages.error(request, "Disputes can only be raised for tasks currently in progress.")
        return redirect('my_tasks')

    if hasattr(task, 'dispute'):
        dispute = task.dispute
        if dispute.status in ['open', 'under_review', 'jury_voting']:
            messages.info(request, "A dispute for this task is already active.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        elif dispute.status in ['resolved', 'closed']:
            messages.error(request, "The dispute for this task has already been resolved or closed.")
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
                    escrow_status='held',
                    status='open'
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

            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != dispute.raised_by and request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to withdraw this dispute.")
        return redirect('my_tasks')

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now back in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def escalate_to_jury(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to escalate this dispute.")
        return redirect('home')

    if dispute.status not in ['open', 'under_review']:
        messages.error(request, "Only open or under-review disputes can be escalated to community jury.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.status = 'jury_voting'
    dispute.save()

    counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
    if counterparty:
        Notification.objects.create(
            recipient=counterparty,
            message=f"The dispute for '{task.title}' has been escalated to Community Jury voting.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, "Dispute escalated to Community Jury voting.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'jury_voting':
        messages.error(request, "This dispute is not currently open for jury voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.can_user_vote(request.user):
        messages.error(request, "You are not eligible to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['favor_poster', 'favor_taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(dispute=dispute, juror=request.user, vote=vote_choice)

        poster_votes = dispute.votes.filter(vote='favor_poster').count()
        taker_votes = dispute.votes.filter(vote='favor_taker').count()
        total_votes = poster_votes + taker_votes

        if total_votes >= JURY_QUORUM:
            dispute.status = 'resolved'
            dispute.save()

            if poster_votes > taker_votes:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                task.status = 'cancelled'
                task.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Jury resolution refund for dispute on task: '{task.title}'"
                )

                decision_msg = f"Community jury resolved dispute for '{task.title}' in favor of task poster ({task.posted_by.username})."
            else:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                task.status = 'completed'
                task.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Jury resolution award for dispute on task: '{task.title}'"
                )

                decision_msg = f"Community jury resolved dispute for '{task.title}' in favor of task taker ({task.taken_by.username})."

            Notification.objects.create(
                recipient=task.posted_by,
                message=decision_msg,
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=decision_msg,
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    messages.success(request, "Your vote has been recorded.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def jury_portal_view(request):
    active_disputes = Dispute.objects.filter(status='jury_voting').order_by('-created_at')
    resolved_disputes = Dispute.objects.filter(status='resolved').order_by('-created_at')[:10]
    
    context = {
        'active_disputes': active_disputes,
        'resolved_disputes': resolved_disputes,
        'quorum': JURY_QUORUM,
    }
    return render(request, 'jury_portal.html', context)
