import math
import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse

from ..models import Dispute, Task, Notification, JuryPanel, DisputeVote, RewardLedger, UserProfile


def impanel_jury(dispute):
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by

    excluded_ids = [posted_by.id]
    if taken_by:
        excluded_ids.append(taken_by.id)

    base_qs = User.objects.exclude(id__in=excluded_ids)

    # Eligibility criteria: account creation >= 7 days old and rewards balance >= 100
    cutoff = timezone.now() - timedelta(days=7)
    eligible_qs = base_qs.filter(date_joined__lte=cutoff, userprofile__rewards__gte=100)

    candidates = list(eligible_qs)
    # Fallback to disinterested users with >= 100 rewards if fewer than 5 meet 7-day age
    if len(candidates) < 5:
        candidates = list(base_qs.filter(userprofile__rewards__gte=100))
    # Fallback to all disinterested users if still fewer than 5
    if len(candidates) < 5:
        candidates = list(base_qs)

    selected_count = min(5, len(candidates))
    selected_jurors = random.sample(candidates, selected_count) if selected_count > 0 else []

    expires_at = timezone.now() + timedelta(hours=48)
    panel, created = JuryPanel.objects.get_or_create(
        dispute=dispute,
        defaults={'expires_at': expires_at}
    )
    if not created:
        panel.expires_at = expires_at
        panel.save()

    panel.jurors.set(selected_jurors)

    # In-app notifications to impaneled jurors with direct link to dispute review portal
    for juror in selected_jurors:
        Notification.objects.create(
            recipient=juror,
            message=f"You have been impaneled as a juror for dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return panel


def resolve_dispute_settlement(panel, winning_choice=None):
    dispute = panel.dispute
    task = dispute.task

    if dispute.status == 'resolved' and panel.status == 'resolved':
        return

    with transaction.atomic():
        counts = panel.vote_counts()
        total_jurors = panel.jurors.count()
        quorum_threshold = min(3, math.floor(total_jurors / 2) + 1) if total_jurors > 0 else 1

        if not winning_choice:
            winner = panel.get_quorum_winner(quorum_threshold=quorum_threshold)
            if winner:
                winning_choice = winner
            elif counts:
                # Plurality fallback
                winning_choice = counts.most_common(1)[0][0]
            else:
                winning_choice = 'split'

        dispute.status = 'resolved'
        dispute.save()

        panel.status = 'resolved' if winning_choice != 'split' or counts else 'expired'
        panel.save()

        reward_amount = task.reward

        if winning_choice == 'poster':
            if task.posted_by:
                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += reward_amount
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=reward_amount,
                    transaction_type='dispute_settlement_poster',
                    description=f"Dispute #{dispute.id} settled in favor of poster (refund)"
                )
        elif winning_choice == 'taker':
            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += reward_amount
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=reward_amount,
                    transaction_type='dispute_settlement_taker',
                    description=f"Dispute #{dispute.id} settled in favor of taker (payout)"
                )
        else:  # 'split'
            doer_share = math.floor(reward_amount / 2)
            poster_share = reward_amount - doer_share

            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += doer_share
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=doer_share,
                    transaction_type='dispute_split',
                    description=f"Dispute #{dispute.id} split settlement (taker share)"
                )

            if task.posted_by:
                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += poster_share
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_share,
                    transaction_type='dispute_split',
                    description=f"Dispute #{dispute.id} split settlement (poster share)"
                )

        # Award participating jurors 20 bonus reward points each
        voted_juror_ids = list(panel.votes.values_list('juror_id', flat=True))
        for juror in User.objects.filter(id__in=voted_juror_ids):
            juror_profile, _ = UserProfile.objects.get_or_create(user=juror)
            juror_profile.rewards += 20
            juror_profile.save()
            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=20,
                transaction_type='juror_reward',
                description=f"Juror participation reward bonus for dispute #{dispute.id}"
            )
            Notification.objects.create(
                recipient=juror,
                message=f"You received 20 bonus points for serving as a juror on dispute for '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        # Notify dispute principals
        choice_display = {
            'poster': 'In Favor of Poster',
            'taker': 'In Favor of Taker',
            'split': 'Refund Both / Split'
        }.get(winning_choice, winning_choice)

        if task.posted_by:
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' has been resolved: {choice_display}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' has been resolved: {choice_display}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    panel = getattr(dispute, 'jury_panel', None)
    is_principal = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = bool(panel and request.user in panel.jurors.all())

    if not is_principal and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    # Expiration check
    if panel and panel.status == 'active' and panel.is_expired():
        resolve_dispute_settlement(panel, winning_choice=None)
        dispute.refresh_from_db()
        panel.refresh_from_db()

    user_voted = False
    if panel:
        user_voted = DisputeVote.objects.filter(panel=panel, juror=request.user).exists()

    chat_messages = []
    if hasattr(task, 'conversation') and task.conversation:
        chat_messages = task.conversation.messages.all()

    vote_count = panel.votes.count() if panel else 0
    total_jurors = panel.jurors.count() if panel else 0

    context = {
        'dispute': dispute,
        'task': task,
        'panel': panel,
        'is_principal': is_principal,
        'is_juror': is_juror,
        'user_voted': user_voted,
        'vote_count': vote_count,
        'total_jurors': total_jurors,
        'chat_messages': chat_messages,
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

        # Impanel 5 disinterested community members
        impanel_jury(dispute)

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. A community jury panel has been impaneled.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully. A community jury panel has been impaneled.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def cast_jury_vote(request, panel_id):
    panel = get_object_or_404(JuryPanel, id=panel_id)
    dispute = panel.dispute
    task = dispute.task

    # Task poster and taker are strictly prohibited from casting ballots
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Dispute principals are strictly prohibited from casting votes.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user not in panel.jurors.all():
        messages.error(request, "You are not an impaneled juror for this dispute.")
        return redirect('home')

    if panel.status != 'active' or dispute.status != 'open':
        messages.error(request, "This jury panel is no longer active.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(panel=panel, juror=request.user).exists():
        messages.info(request, "You have already cast your ballot for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote_choice')
    if vote_choice not in ['poster', 'taker', 'split']:
        messages.error(request, "Invalid voting option selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        panel=panel,
        juror=request.user,
        vote_choice=vote_choice
    )
    messages.success(request, "Your confidential ballot has been recorded.")

    # Check for simple majority quorum
    total_jurors = panel.jurors.count()
    quorum_threshold = min(3, math.floor(total_jurors / 2) + 1) if total_jurors > 0 else 1
    winner = panel.get_quorum_winner(quorum_threshold=quorum_threshold)

    if winner:
        resolve_dispute_settlement(panel, winning_choice=winner)
    elif panel.votes.count() >= total_jurors:
        resolve_dispute_settlement(panel, winning_choice=None)

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
