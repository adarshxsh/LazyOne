import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import (
    Dispute, Task, Notification, RewardLedger,
    JuryPool, JuryVote, UserProfile, Friendship, FriendRequest
)


def select_jury_for_dispute(dispute):
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by

    excluded_user_ids = set()
    if posted_by:
        excluded_user_ids.add(posted_by.id)
    if taken_by:
        excluded_user_ids.add(taken_by.id)

    # Exclude friends of posted_by
    if posted_by and hasattr(posted_by, 'userprofile'):
        p_profile = posted_by.userprofile
        excluded_user_ids.update(p_profile.friends.values_list('user_id', flat=True))
        excluded_user_ids.update(User.objects.filter(userprofile__friends=p_profile).values_list('id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friendship_from_user__to_user=p_profile).values_list('user_id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friendship_to_user__from_user=p_profile).values_list('user_id', flat=True))
        excluded_user_ids.update(User.objects.filter(from_user__to_user=posted_by, from_user__is_accepted=True).values_list('id', flat=True))
        excluded_user_ids.update(User.objects.filter(to_user__from_user=posted_by, to_user__is_accepted=True).values_list('id', flat=True))

    # Exclude friends of taken_by
    if taken_by and hasattr(taken_by, 'userprofile'):
        t_profile = taken_by.userprofile
        excluded_user_ids.update(t_profile.friends.values_list('user_id', flat=True))
        excluded_user_ids.update(User.objects.filter(userprofile__friends=t_profile).values_list('id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friendship_from_user__to_user=t_profile).values_list('user_id', flat=True))
        excluded_user_ids.update(UserProfile.objects.filter(friendship_to_user__from_user=t_profile).values_list('user_id', flat=True))
        excluded_user_ids.update(User.objects.filter(from_user__to_user=taken_by, from_user__is_accepted=True).values_list('id', flat=True))
        excluded_user_ids.update(User.objects.filter(to_user__from_user=taken_by, to_user__is_accepted=True).values_list('id', flat=True))

    # Candidate users: active users with reward balance >= 100
    candidate_users = list(User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=100
    ).exclude(id__in=excluded_user_ids))

    num_jurors = min(3, len(candidate_users))
    selected_jurors = random.sample(candidate_users, num_jurors) if num_jurors > 0 else []

    JuryPool.objects.filter(dispute=dispute).delete()

    for juror in selected_jurors:
        JuryPool.objects.create(dispute=dispute, juror=juror)
        Notification.objects.create(
            recipient=juror,
            message=f"You have been selected as a peer juror for a dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return selected_jurors


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_disputant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = JuryPool.objects.filter(dispute=dispute, juror=request.user).exists()

    if not is_disputant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    poster_votes = JuryVote.objects.filter(dispute=dispute, vote='poster').count()
    taker_votes = JuryVote.objects.filter(dispute=dispute, vote='taker').count()
    user_vote = JuryVote.objects.filter(dispute=dispute, juror=request.user).first()
    has_voted = user_vote is not None

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': poster_votes + taker_votes,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if (request.user != task.taken_by and request.user != task.posted_by) or task.status != 'in_progress':
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

            counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            select_jury_for_dispute(dispute)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Disputants cannot vote on their own dispute
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Disputants cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Must be in jury pool
    if not JuryPool.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Cannot vote twice
    if JuryVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote') or request.POST.get('choice')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JuryVote.objects.create(dispute=dispute, juror=request.user, vote=vote_choice)

        poster_votes = JuryVote.objects.filter(dispute=dispute, vote='poster').count()
        taker_votes = JuryVote.objects.filter(dispute=dispute, vote='taker').count()

        if poster_votes >= 2:
            # Poster wins simple majority
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for cancelled task via dispute resolution: '{task.title}'"
            )

            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit()
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by)

            task.status = 'cancelled'
            task.save()
            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Jury voted in your favor. Dispute resolved for task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Jury voted in favor of poster. Dispute resolved for task '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        elif taker_votes >= 2:
            # Taker wins simple majority
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task via dispute resolution: '{task.title}'"
                )

                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit()
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by)

            task.status = 'completed'
            task.save()
            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Jury voted in favor of taker. Dispute resolved for task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Jury voted in your favor. Dispute resolved for task '{task.title}'.",
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

        JuryPool.objects.filter(dispute=dispute).delete()
        JuryVote.objects.filter(dispute=dispute).delete()

        task.status = 'in_progress'
        task.save()

        counterparty = task.posted_by if request.user != task.posted_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

