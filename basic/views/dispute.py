import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse

from ..models import Dispute, Task, Notification, JuryAssignment, DisputeVote, RewardLedger, Friendship, FriendRequest


def assemble_jury_panel(dispute, panel_size=3):
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by

    excluded_user_ids = {posted_by.id}
    if taken_by:
        excluded_user_ids.add(taken_by.id)

    # Exclude friends of participants
    for participant in [posted_by, taken_by]:
        if not participant:
            continue
        profile = getattr(participant, 'userprofile', None)
        if profile:
            excluded_user_ids.update(profile.friends.values_list('user_id', flat=True))

            from_friends = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
            to_friends = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(from_friends)
            excluded_user_ids.update(to_friends)

        fr_sent = FriendRequest.objects.filter(from_user=participant, is_accepted=True).values_list('to_user_id', flat=True)
        fr_recv = FriendRequest.objects.filter(to_user=participant, is_accepted=True).values_list('from_user_id', flat=True)
        excluded_user_ids.update(fr_sent)
        excluded_user_ids.update(fr_recv)

    eligible_users = list(User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids))

    num_to_select = min(panel_size, len(eligible_users))
    if num_to_select > 0:
        selected_jurors = random.sample(eligible_users, num_to_select)
    else:
        selected_jurors = []

    juror_assignments = []
    for juror in selected_jurors:
        assignment = JuryAssignment.objects.create(dispute=dispute, user=juror)
        juror_assignments.append(assignment)
        Notification.objects.create(
            recipient=juror,
            message=f"You have been selected as a juror for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
    return juror_assignments


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_juror = JuryAssignment.objects.filter(dispute=dispute, user=request.user).exists()
    is_participant = (request.user == task.posted_by or (task.taken_by and request.user == task.taken_by))

    if not is_participant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
    can_vote = is_juror and dispute.status == 'open' and user_vote is None

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'user_vote': user_vote,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if (request.user != task.posted_by and request.user != task.taken_by) or task.status != 'in_progress':
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

            assemble_jury_panel(dispute, panel_size=3)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not JuryAssignment.objects.filter(dispute=dispute, user=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            choice=choice
        )

        poster_votes = dispute.votes.filter(choice='poster').count()
        taker_votes = dispute.votes.filter(choice='taker').count()

        panel_size = dispute.jury_assignments.count()
        majority_threshold = (panel_size // 2) + 1 if panel_size > 0 else 1

        if poster_votes >= majority_threshold or taker_votes >= majority_threshold:
            if poster_votes > taker_votes:
                winner = task.posted_by
                task.status = 'cancelled'
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for disputed task: '{task.title}'"
                )
            else:
                winner = task.taken_by
                task.status = 'completed'
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward for completed disputed task: '{task.title}'"
                )

            task.save()
            dispute.status = 'resolved'
            dispute.save()

            if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                if dispute.raised_by == winner:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=winner,
                        reason_description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                    )

            # Notifications to assigned jurors and disputing participants
            recipients = set(dispute.jury_assignments.values_list('user_id', flat=True))
            recipients.add(task.posted_by.id)
            if task.taken_by:
                recipients.add(task.taken_by.id)

            for recipient_id in recipients:
                Notification.objects.create(
                    recipient_id=recipient_id,
                    message=f"Dispute for task '{task.title}' has been resolved in favor of {winner.username}.",
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

        counterparty = task.posted_by if request.user == task.taken_by else task.taken_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
