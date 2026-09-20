from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeEvidence, DisputeVote, DisputeAppeal


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Task participants, staff, or peer reviewers during voting phase can view details
    if dispute.status != 'voting_phase' and request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evidence_entries = dispute.evidence_entries.all().order_by('created_at')
    votes = dispute.votes.all().order_by('created_at')
    appeals = dispute.appeals.all().order_by('created_at')

    poster_votes = votes.filter(choice='poster').count()
    taker_votes = votes.filter(choice='taker').count()

    is_participant = request.user in [task.posted_by, task.taken_by]
    has_voted = votes.filter(voter=request.user).exists()

    can_submit_evidence = is_participant and dispute.status == 'evidence_phase'
    can_vote = (not is_participant) and dispute.status == 'voting_phase' and not has_voted
    can_appeal = is_participant and dispute.status == 'appeal_phase'

    context = {
        'dispute': dispute,
        'task': task,
        'evidence_entries': evidence_entries,
        'votes': votes,
        'appeals': appeals,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'can_submit_evidence': can_submit_evidence,
        'can_vote': can_vote,
        'can_appeal': can_appeal,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'evidence_phase', 'voting_phase', 'appeal_phase']:
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
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

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by

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

            # Move dispute into evidence submission phase
            dispute.transition_to('evidence_phase')

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

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
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status in ['settled', 'withdrawn', 'resolved']:
        messages.error(request, "This dispute is already finalized and cannot be withdrawn.")
        return redirect('my_tasks')

    task = dispute.task
    counterparty = task.posted_by if request.user == task.taken_by else task.taken_by

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'withdrawn'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can submit evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    if dispute.status != 'evidence_phase':
        messages.error(request, "Evidence can only be submitted during the evidence submission phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    description = request.POST.get('description', '').strip()
    evidence_url = request.POST.get('evidence_url', '').strip()
    file_attachment = request.FILES.get('file')

    if not description and not evidence_url and not file_attachment:
        messages.error(request, "Please provide evidence details (description, URL, or file attachment).")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        description=description,
        evidence_url=evidence_url,
        file=file_attachment
    )
    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def submit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user in [task.posted_by, task.taken_by]:
        messages.error(request, "Task participants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    if dispute.status != 'voting_phase':
        messages.error(request, "Votes can only be submitted during the voting phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    justification = request.POST.get('justification', '').strip()
    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        choice=choice,
        justification=justification
    )
    messages.success(request, "Your vote has been cast successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user not in [task.posted_by, task.taken_by]:
        messages.error(request, "Only task participants can file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    if dispute.status != 'appeal_phase':
        messages.error(request, "Appeals can only be submitted during the appeal phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, "A reason/justification is required to submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeAppeal.objects.create(
        dispute=dispute,
        appellant=request.user,
        reason=reason
    )
    messages.success(request, "Appeal filed successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def advance_dispute_phase_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user not in [task.posted_by, task.taken_by] and not request.user.is_staff:
        messages.error(request, "You are not authorized to advance this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        advance_dispute_phase(dispute)
        messages.success(request, f"Dispute advanced to phase: {dispute.get_status_display()}")
    except Exception as e:
        messages.error(request, f"Could not advance dispute: {str(e)}")

    return redirect('dispute_detail', dispute_id=dispute.id)


def advance_dispute_phase(dispute):
    if dispute.status == 'open':
        dispute.transition_to('evidence_phase')
    elif dispute.status == 'evidence_phase':
        dispute.transition_to('voting_phase')
    elif dispute.status == 'voting_phase':
        dispute.transition_to('appeal_phase')
    elif dispute.status == 'appeal_phase':
        settle_dispute(dispute)


def settle_dispute(dispute):
    task = dispute.task
    if dispute.status == 'settled':
        return

    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()

    with transaction.atomic():
        if poster_votes > taker_votes:
            # Poster wins: cancel task, refund task reward to poster, refund or forfeit bond
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Reward refunded upon dispute settlement for task: '{task.title}'"
            )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded upon dispute settlement for task '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited to poster upon dispute settlement for task '{task.title}'")

            task.status = 'cancelled'
            task.save()
        else:
            # Taker wins or default: complete task, transfer reward to taker
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward upon dispute settlement for task: '{task.title}'"
                )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded upon dispute settlement for task '{task.title}'")
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited to taker upon dispute settlement for task '{task.title}'")

            task.status = 'completed'
            task.save()

        if dispute.escrow_status == 'held':
            dispute.escrow_status = 'disbursed'

        dispute.transition_to('settled')
