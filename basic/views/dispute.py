from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from ..models import Dispute, Task, Notification, RewardLedger, Jury, JuryVote, create_jury_for_dispute
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    jury = getattr(dispute, 'jury', None)

    is_poster_or_taker = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = jury and jury.jurors.filter(id=request.user.id).exists()

    if not is_poster_or_taker and not request.user.is_staff and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    has_voted = is_juror and JuryVote.objects.filter(jury=jury, juror=request.user).exists()
    user_vote = JuryVote.objects.filter(jury=jury, juror=request.user).first() if is_juror else None
    show_tallies = (dispute.status == 'resolved') or has_voted or is_poster_or_taker or request.user.is_staff

    poster_votes = JuryVote.objects.filter(jury=jury, vote='poster_wins').count() if jury else 0
    taker_votes = JuryVote.objects.filter(jury=jury, vote='taker_wins').count() if jury else 0
    total_votes = JuryVote.objects.filter(jury=jury).count() if jury else 0

    context = {
        'dispute': dispute,
        'task': task,
        'jury': jury,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'show_tallies': show_tallies,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
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

            create_jury_for_dispute(dispute)

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
def cast_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        return HttpResponseForbidden("Task poster and taker cannot cast juror votes.")

    jury = getattr(dispute, 'jury', None)
    if not jury or not jury.jurors.filter(id=request.user.id).exists():
        return HttpResponseForbidden("You are not an assigned juror for this dispute.")

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JuryVote.objects.filter(jury=jury, juror=request.user).exists():
        messages.error(request, "You have already cast your vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster_wins', 'taker_wins']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JuryVote.objects.create(
            jury=jury,
            juror=request.user,
            vote=vote_choice
        )

        total_jurors = jury.jurors.count()
        majority_threshold = (total_jurors // 2) + 1 if total_jurors > 0 else 1

        poster_wins_count = JuryVote.objects.filter(jury=jury, vote='poster_wins').count()
        taker_wins_count = JuryVote.objects.filter(jury=jury, vote='taker_wins').count()

        if poster_wins_count >= majority_threshold:
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
                description=f"Refund reserved reward points for disputed task: '{task.title}'"
            )

            dispute.forfeit_deposit(
                beneficiary=task.posted_by,
                reason_description=f"Security deposit bond forfeited to poster for dispute on task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Jury consensus reached for dispute on task '{task.title}': Poster Wins.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Jury consensus reached for dispute on task '{task.title}': Poster Wins.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        elif taker_wins_count >= majority_threshold:
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
                    description=f"Task reward awarded via jury consensus for task: '{task.title}'"
                )

            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded via jury consensus for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Jury consensus reached for dispute on task '{task.title}': Taker Wins.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Jury consensus reached for dispute on task '{task.title}': Taker Wins.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, "Your vote has been submitted successfully.")

    return redirect('dispute_detail', dispute_id=dispute.id)

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
