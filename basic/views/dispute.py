import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, JuryAssignment, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

JURY_PARTICIPATION_BONUS = 25

def select_jury_for_dispute(dispute):
    task = dispute.task
    excluded_user_ids = [task.posted_by.id]
    if task.taken_by:
        excluded_user_ids.append(task.taken_by.id)

    # Filter active users with non-negative rewards excluding poster and taker
    eligible_users = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=0
    ).exclude(id__in=excluded_user_ids)

    user_list = list(eligible_users)
    num_to_select = min(3, len(user_list))
    selected_users = random.sample(user_list, num_to_select)

    dispute_link = reverse('dispute_detail', args=[dispute.id])
    for juror in selected_users:
        assignment, created = JuryAssignment.objects.get_or_create(
            dispute=dispute,
            user=juror
        )
        if created:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been selected as a peer juror for a task dispute: '{task.title}'. Please review and vote.",
                link=dispute_link
            )

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_poster = (request.user == task.posted_by)
    is_taker = (request.user == task.taken_by)
    is_juror = JuryAssignment.objects.filter(dispute=dispute, user=request.user).exists()
    is_staff = request.user.is_staff

    if not (is_poster or is_taker or is_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_has_voted = DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists() if is_juror else False
    can_vote = is_juror and (dispute.status == 'open') and (not dispute.is_voting_expired) and (not user_has_voted)

    task_messages = task.main_chat.messages.all() if task.main_chat else []

    context = {
        'dispute': dispute,
        'task': task,
        'is_poster': is_poster,
        'is_taker': is_taker,
        'is_juror': is_juror,
        'is_staff': is_staff,
        'user_has_voted': user_has_voted,
        'can_vote': can_vote,
        'task_messages': task_messages,
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

            # Assign random disinterested peer jury
            select_jury_for_dispute(dispute)

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. A 3-member peer jury has been assigned.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.is_voting_expired:
        messages.error(request, "The 48-hour voting window for this dispute has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not JuryAssignment.objects.filter(dispute=dispute, user=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_user_id = request.POST.get('voted_user')
    if not voted_user_id:
        messages.error(request, "Please select a party to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        voted_user_id = int(voted_user_id)
    except ValueError:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    valid_targets = [task.posted_by.id]
    if task.taken_by:
        valid_targets.append(task.taken_by.id)

    if voted_user_id not in valid_targets:
        messages.error(request, "Invalid vote target.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_user = User.objects.get(id=voted_user_id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            vote_for=voted_user
        )

        # Award fixed participation reward bonus
        juror_profile = request.user.userprofile
        juror_profile.rewards += JURY_PARTICIPATION_BONUS
        juror_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=JURY_PARTICIPATION_BONUS,
            transaction_type='jury_reward',
            description=f"Participation reward bonus for serving as juror on dispute: '{task.title}'"
        )

        # Check if majority consensus reached
        dispute.check_consensus_and_settle()

    messages.success(request, f"Your vote has been recorded. You earned {JURY_PARTICIPATION_BONUS} reward points as a jury bonus!")
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

