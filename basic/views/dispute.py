from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse

from ..models import Dispute, Task, Notification, RewardLedger, DisputeJuror
from ..utils import select_jurors_for_dispute


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = (request.user == task.posted_by or request.user == task.taken_by or request.user == dispute.raised_by)
    juror_record = dispute.dispute_jurors.filter(user=request.user).first()
    is_assigned_juror = (juror_record is not None)
    is_staff = request.user.is_staff or request.user.is_superuser

    if not (is_participant or is_assigned_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    hide_vote_details = (dispute.status == 'open' and is_participant and not is_staff and not is_assigned_juror)

    total_votes = dispute.dispute_jurors.exclude(vote='pending').count()
    poster_votes = dispute.dispute_jurors.filter(vote='poster').count() if not hide_vote_details else None
    taker_votes = dispute.dispute_jurors.filter(vote='taker').count() if not hide_vote_details else None

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'is_assigned_juror': is_assigned_juror,
        'user_juror_record': juror_record,
        'hide_vote_details': hide_vote_details,
        'total_votes': total_votes,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'jurors': dispute.dispute_jurors.all() if not hide_vote_details else []
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

            # Assign 3 neutral weighted jurors
            select_jurors_for_dispute(dispute, count=3)

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


def resolve_dispute_automated(dispute, winner):
    task = dispute.task
    dispute.status = 'resolved'
    dispute.save()

    if winner == 'poster':
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
            description=f"Task reward refunded upon winning dispute on task: '{task.title}'"
        )

        dispute.forfeit_deposit(
            beneficiary=task.posted_by,
            reason_description=f"Deposit bond awarded to poster upon winning dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' was resolved in your favor.",
            link=reverse('my_tasks')
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of the task poster.",
                link=reverse('my_tasks')
            )

    elif winner == 'taker':
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
                description=f"Task reward awarded upon winning dispute on task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor.",
                link=reverse('my_tasks')
            )

        dispute.refund_deposit(
            reason_description=f"Deposit bond refunded upon winning dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' was resolved in favor of the task taker.",
            link=reverse('my_tasks')
        )


@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    juror_record = DisputeJuror.objects.filter(dispute=dispute, user=request.user).first()

    if not juror_record:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    reasoning = request.POST.get('reasoning', '')

    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice. Must be 'poster' or 'taker'.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_record.vote = vote_choice
        juror_record.reasoning = reasoning
        juror_record.voted_at = timezone.now()
        juror_record.save()

        poster_votes = dispute.dispute_jurors.filter(vote='poster').count()
        taker_votes = dispute.dispute_jurors.filter(vote='taker').count()
        total_cast = poster_votes + taker_votes

        if poster_votes >= 2:
            resolve_dispute_automated(dispute, winner='poster')
        elif taker_votes >= 2:
            resolve_dispute_automated(dispute, winner='taker')
        elif total_cast == 3:
            winner = 'poster' if poster_votes > taker_votes else 'taker'
            resolve_dispute_automated(dispute, winner=winner)

    messages.success(request, "Your vote has been recorded.")
    return redirect('dispute_detail', dispute_id=dispute.id)
