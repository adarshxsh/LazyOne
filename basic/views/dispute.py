import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from ..models import (
    Dispute, Task, Notification, JuryAssignment, DisputeVote,
    UserProfile, Friendship, FriendRequest, RewardLedger
)

def get_direct_friends_ids(user):
    friend_ids = set()
    if not user:
        return friend_ids
    profile = UserProfile.objects.filter(user=user).first()
    if profile:
        for f in profile.friends.all():
            friend_ids.add(f.user_id)
        friendships = Friendship.objects.filter(Q(from_user=profile) | Q(to_user=profile))
        for fs in friendships:
            if fs.from_user != profile and fs.from_user.user:
                friend_ids.add(fs.from_user.user.id)
            if fs.to_user != profile and fs.to_user.user:
                friend_ids.add(fs.to_user.user.id)
    frs = FriendRequest.objects.filter(Q(from_user=user) | Q(to_user=user), is_accepted=True)
    for fr in frs:
        if fr.from_user != user:
            friend_ids.add(fr.from_user.id)
        if fr.to_user != user:
            friend_ids.add(fr.to_user.id)
    return friend_ids

def assign_jury_pool(dispute, target_pool_size=3):
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by

    excluded_ids = set()
    if posted_by:
        excluded_ids.add(posted_by.id)
        excluded_ids.update(get_direct_friends_ids(posted_by))
    if taken_by:
        excluded_ids.add(taken_by.id)
        excluded_ids.update(get_direct_friends_ids(taken_by))

    eligible_users = list(User.objects.filter(is_active=True).exclude(id__in=excluded_ids))

    available_count = len(eligible_users)
    if available_count == 0:
        actual_size = 0
    elif available_count < target_pool_size:
        actual_size = available_count if available_count % 2 == 1 else available_count - 1
    else:
        actual_size = target_pool_size if target_pool_size % 2 == 1 else target_pool_size - 1

    if actual_size > 0:
        selected_users = random.sample(eligible_users, actual_size)
        jury_assignments = []
        for user in selected_users:
            jury_assignments.append(JuryAssignment(dispute=dispute, juror=user))
            chat_link = reverse('chat_view', args=[task.conversation.id]) if hasattr(task, 'conversation') and task.conversation else reverse('dispute_detail', args=[dispute.id])
            Notification.objects.create(
                recipient=user,
                message=f"You have been impaneled as a juror for dispute on task '{task.title}'.",
                link=chat_link
            )
        JuryAssignment.objects.bulk_create(jury_assignments, ignore_conflicts=True)

@transaction.atomic
def resolve_dispute_by_jury(dispute, winner):
    if dispute.status == 'resolved':
        return
    task = dispute.task
    reward = task.reward

    if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
        if winner == 'poster':
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(reason_description=f"Deposit bond refunded for winning dispute on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'")
        elif winner == 'taker':
            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(reason_description=f"Deposit bond refunded for winning dispute on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'")

    if winner == 'poster':
        poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
        poster_profile.rewards += reward
        poster_profile.save()

        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=reward,
            transaction_type='task_cancellation',
            description=f"Dispute refund for task: {task.title}"
        )
        task.status = 'cancelled'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Jury verdict rendered in your favor for task '{task.title}'. {reward} points refunded.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Jury verdict rendered for task '{task.title}' in favor of task poster.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    elif winner == 'taker':
        if task.taken_by:
            taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
            taker_profile.rewards += reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=reward,
                transaction_type='task_completion',
                description=f"Dispute payout for task: {task.title}"
            )
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Jury verdict rendered in your favor for task '{task.title}'. {reward} points awarded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        task.status = 'completed'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Jury verdict rendered for task '{task.title}' in favor of task doer.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    dispute.status = 'resolved'
    dispute.save()

    for assignment in dispute.jury_assignments.all():
        Notification.objects.create(
            recipient=assignment.juror,
            message=f"Dispute on task '{task.title}' has been resolved by jury consensus.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    is_staff = request.user.is_staff

    if not is_participant and not is_juror and not is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    is_expired = timezone.now() > dispute.created_at + timedelta(hours=48)
    if dispute.status == 'open' and is_expired:
        staff_users = User.objects.filter(is_staff=True)
        for s in staff_users:
            notification_msg = f"Dispute on task '{task.title}' expired without jury quorum. Staff escalation required."
            if not Notification.objects.filter(recipient=s, message=notification_msg).exists():
                Notification.objects.create(
                    recipient=s,
                    message=notification_msg,
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    has_voted = dispute.votes.filter(voter=request.user).exists()
    can_vote = is_juror and not has_voted and dispute.status == 'open' and not is_expired

    show_results = has_voted or is_participant or is_staff or dispute.status == 'resolved'

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'can_vote': can_vote,
        'show_results': show_results,
        'poster_votes': dispute.votes.filter(choice='poster').count() if show_results else None,
        'taker_votes': dispute.votes.filter(choice='taker').count() if show_results else None,
        'total_votes': dispute.votes.count() if show_results else None,
        'is_expired': is_expired,
        'user_vote': dispute.votes.filter(voter=request.user).first() if has_voted else None,
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

            assign_jury_pool(dispute)

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
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    
    if not dispute.jury_assignments.filter(juror=request.user).exists():
        messages.error(request, "You are not an impaneled juror for this dispute.")
        return redirect('home')

    is_expired = timezone.now() > dispute.created_at + timedelta(hours=48)
    if dispute.status != 'open' or is_expired:
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    reason = request.POST.get('reason', '')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        voter=request.user,
        choice=choice,
        reason=reason
    )
    messages.success(request, "Your vote has been recorded successfully.")

    juror_count = dispute.jury_assignments.count()
    if juror_count == 0:
        juror_count = 1
    majority_threshold = (juror_count // 2) + 1

    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()

    if poster_votes >= majority_threshold:
        resolve_dispute_by_jury(dispute, 'poster')
    elif taker_votes >= majority_threshold:
        resolve_dispute_by_jury(dispute, 'taker')

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

