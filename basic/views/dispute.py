from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JuryPanel, JurorVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    jury_panel = getattr(dispute, 'jury_panel', None)

    # Automatically ensure a jury panel exists if dispute is open
    if dispute.status == 'open' and not jury_panel:
        jury_panel = JuryPanel.create_panel_for_dispute(dispute)

    is_juror = jury_panel.is_juror(request.user) if jury_panel else False
    is_litigant = (request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by)

    if not is_litigant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    has_voted = jury_panel.has_voted(request.user) if (jury_panel and is_juror) else False
    user_vote = jury_panel.get_vote(request.user) if (jury_panel and is_juror) else None

    # Requirement 2: Selected jurors must NOT see active vote counts before voting
    show_vote_counts = True
    if is_juror and not has_voted and dispute.status == 'open' and not request.user.is_staff:
        show_vote_counts = False

    poster_votes = 0
    taker_votes = 0
    if jury_panel and show_vote_counts:
        poster_votes = jury_panel.votes.filter(vote='poster').count()
        taker_votes = jury_panel.votes.filter(vote='taker').count()

    context = {
        'dispute': dispute,
        'task': task,
        'jury_panel': jury_panel,
        'is_juror': is_juror,
        'is_litigant': is_litigant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'show_vote_counts': show_vote_counts,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if (task.taken_by != request.user and task.posted_by != request.user) or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are involved in that is currently in progress.")
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

            # Requirement 1: Create Jury Panel with 3+ neutral jurors
            JuryPanel.create_panel_for_dispute(dispute)

            other_party = task.posted_by if request.user == task.taken_by else task.taken_by
            if other_party:
                Notification.objects.create(
                    recipient=other_party,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Neutral jury assigned.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    jury_panel = getattr(dispute, 'jury_panel', None)

    if not jury_panel or not jury_panel.is_juror(request.user):
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if jury_panel.status != 'voting' or dispute.status == 'resolved':
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if jury_panel.has_voted(request.user):
        messages.info(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    comments = request.POST.get('comments', '')

    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JurorVote.objects.create(
            panel=jury_panel,
            juror=request.user,
            vote=vote_choice,
            comments=comments
        )
        messages.success(request, "Your vote has been submitted successfully.")

        # Check consensus: if all assigned jurors have voted
        total_jurors_count = jury_panel.jurors.count()
        total_votes_count = jury_panel.votes.count()
        if total_votes_count >= total_jurors_count and total_jurors_count > 0:
            jury_panel.tally_and_settle()
            messages.info(request, "All assigned jurors have voted. The dispute consensus outcome has been settled!")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def jury_portal_view(request):
    assigned_panels = request.user.assigned_jury_panels.select_related('dispute__task').order_by('-created_at')
    active_panels = assigned_panels.filter(status='voting')
    resolved_panels = assigned_panels.filter(status='resolved')

    context = {
        'active_panels': active_panels,
        'resolved_panels': resolved_panels,
    }
    return render(request, 'jury_portal.html', context)

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

        if hasattr(dispute, 'jury_panel'):
            jury_panel = dispute.jury_panel
            jury_panel.status = 'cancelled'
            jury_panel.save()

        task.status = 'in_progress'
        task.save()

        other_party = task.posted_by if request.user == task.taken_by else task.taken_by
        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
