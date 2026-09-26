from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..services.juror import (
    select_and_assign_jurors,
    check_and_replace_expired_jurors,
    process_juror_vote
)

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_disputant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = dispute.jurors.filter(user=request.user).exists()

    if not is_disputant and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    check_and_replace_expired_jurors(dispute)

    is_juror = dispute.jurors.filter(user=request.user).exists()
    juror_assignment = dispute.jurors.filter(user=request.user).first() if is_juror else None
    can_vote = bool(is_juror and juror_assignment and juror_assignment.vote == 'pending' and dispute.status == 'open')

    conversation = getattr(task, 'conversation', None)

    anonymized_jurors = []
    if is_disputant and dispute.status == 'open':
        for i, j in enumerate(dispute.jurors.all()):
            anonymized_jurors.append({
                'label': f"Juror {i+1}",
                'status': 'Voted' if j.vote != 'pending' else 'Pending'
            })

    context = {
        'dispute': dispute,
        'task': task,
        'is_disputant': is_disputant,
        'is_juror': is_juror,
        'juror_assignment': juror_assignment,
        'can_vote': can_vote,
        'conversation': conversation,
        'jurors': dispute.jurors.all(),
        'anonymized_jurors': anonymized_jurors,
    }
    return render(request, 'dispute_detail.html', context)

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

        assigned_jurors = select_and_assign_jurors(dispute)
        if not assigned_jurors:
            messages.warning(
                request,
                f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Notice: Insufficient neutral community members were available for auto-assignment. Dispute queued for staff review."
            )
        else:
            messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. 3 neutral community jurors assigned.")

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

@login_required(login_url='/login/')
@require_POST
def submit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    juror_assignment = dispute.jurors.filter(user=request.user).first()
    if not juror_assignment:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if juror_assignment.vote != 'pending':
        messages.error(request, "You have already submitted your vote. Votes cannot be changed once submitted.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    reasoning = request.POST.get('reasoning', '').strip()

    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        process_juror_vote(juror_assignment, vote_choice, reasoning)
        messages.success(request, "Your vote has been submitted successfully.")
    except Exception as e:
        messages.error(request, f"Error submitting vote: {str(e)}")

    return redirect('dispute_detail', dispute_id=dispute.id)

