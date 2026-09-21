import math
import random
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, Jury, JurorAssignment, JuryVote, UserProfile


def sample_and_assign_jurors(dispute, count=5):
    task = dispute.task
    exclude_user_ids = set()

    # Exclude task poster and taker
    if task.posted_by_id:
        exclude_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        exclude_user_ids.add(task.taken_by_id)

    # Exclude users involved in active (open) disputes
    open_disputes = Dispute.objects.filter(status='open')
    for d in open_disputes:
        if d.raised_by_id:
            exclude_user_ids.add(d.raised_by_id)
        if d.task and d.task.posted_by_id:
            exclude_user_ids.add(d.task.posted_by_id)
        if d.task and d.task.taken_by_id:
            exclude_user_ids.add(d.task.taken_by_id)

    # Exclude friends of posted_by
    if hasattr(task.posted_by, 'userprofile'):
        poster_friends = task.posted_by.userprofile.friends.values_list('user_id', flat=True)
        exclude_user_ids.update(poster_friends)

    # Exclude friends of taken_by
    if task.taken_by and hasattr(task.taken_by, 'userprofile'):
        taker_friends = task.taken_by.userprofile.friends.values_list('user_id', flat=True)
        exclude_user_ids.update(taker_friends)

    # Exclude users already assigned to this jury if jury exists
    if hasattr(dispute, 'jury'):
        assigned_user_ids = dispute.jury.assignments.values_list('juror_id', flat=True)
        exclude_user_ids.update(assigned_user_ids)

    candidate_users = list(User.objects.filter(is_active=True).exclude(id__in=exclude_user_ids))
    sampled_users = random.sample(candidate_users, min(count, len(candidate_users)))

    jury, created = Jury.objects.get_or_create(
        dispute=dispute,
        defaults={
            'status': 'voting',
            'deadline': timezone.now() + timedelta(hours=48)
        }
    )
    if not created and jury.status != 'concluded':
        jury.status = 'voting'
        if not jury.deadline or jury.deadline <= timezone.now():
            jury.deadline = timezone.now() + timedelta(hours=48)
        jury.save()

    for juror in sampled_users:
        JurorAssignment.objects.get_or_create(
            jury=jury,
            juror=juror,
            defaults={'status': 'assigned'}
        )
        Notification.objects.create(
            recipient=juror,
            message=f"You have been randomly selected as a juror for the dispute on task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return jury


def check_and_handle_jury_timeouts(jury):
    if jury.status == 'voting' and jury.deadline and timezone.now() >= jury.deadline:
        expired_assignments = jury.assignments.filter(status='assigned')
        for assignment in expired_assignments:
            assignment.status = 'expired'
            assignment.save()
            Notification.objects.create(
                recipient=assignment.juror,
                message=f"Your juror assignment for dispute on task: '{jury.dispute.task.title}' has expired.",
                link=reverse('dispute_detail', args=[jury.dispute.id])
            )

        active_assignments_count = jury.assignments.filter(status__in=['assigned', 'voted']).count()
        if active_assignments_count < 5:
            needed = 5 - active_assignments_count
            sample_and_assign_jurors(jury.dispute, count=needed)

        process_jury_consensus(jury, allow_plurality_on_timeout=True)


def process_jury_consensus(jury, allow_plurality_on_timeout=False):
    if jury.status == 'concluded':
        return False

    task = jury.dispute.task
    posted_by_votes = jury.votes.filter(voted_for=task.posted_by).count()
    taken_by_votes = jury.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    majority_threshold = 3

    winner = None
    if posted_by_votes >= majority_threshold:
        winner = task.posted_by
    elif taken_by_votes >= majority_threshold:
        winner = task.taken_by
    elif allow_plurality_on_timeout and (posted_by_votes > 0 or taken_by_votes > 0):
        if posted_by_votes > taken_by_votes:
            winner = task.posted_by
        elif taken_by_votes > posted_by_votes:
            winner = task.taken_by

    if not winner:
        return False

    with transaction.atomic():
        jury.status = 'concluded'
        jury.winner = winner
        jury.save()

        dispute = jury.dispute
        if winner == task.taken_by:
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon winning dispute via jury consensus for task: '{task.title}'"
            )
            task_doer_profile = task.taken_by.userprofile
            task_doer_profile.rewards += task.reward
            task_doer_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded by jury consensus for task: '{task.title}'"
            )
            task.status = 'completed'
            task.save()
            dispute.status = 'resolved'
            dispute.save()
        else:
            dispute.forfeit_deposit(
                beneficiary=task.posted_by,
                reason_description=f"Security deposit bond forfeited upon losing dispute via jury consensus for task: '{task.title}'"
            )
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for disputed task awarded by jury consensus: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()
            dispute.status = 'resolved'
            dispute.save()

        # Award juror rewards (20 points) to participating majority voters
        JUROR_REWARD_AMOUNT = 20
        winning_votes = jury.votes.filter(voted_for=winner)
        for vote in winning_votes:
            assignment = JurorAssignment.objects.filter(jury=jury, juror=vote.juror).first()
            if assignment and assignment.status == 'voted' and not assignment.reward_paid:
                juror_profile = vote.juror.userprofile
                juror_profile.rewards += JUROR_REWARD_AMOUNT
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.juror,
                    task=task,
                    amount=JUROR_REWARD_AMOUNT,
                    transaction_type='juror_reward',
                    description=f"Reward for majority jury vote on dispute: '{task.title}'"
                )
                assignment.reward_paid = True
                assignment.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Jury consensus reached for dispute on '{task.title}'. Verdict winner: {winner.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Jury consensus reached for dispute on '{task.title}'. Verdict winner: {winner.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if hasattr(dispute, 'jury'):
        check_and_handle_jury_timeouts(dispute.jury)

    is_party = (request.user == task.posted_by or request.user == task.taken_by or request.user.is_staff)
    is_juror = hasattr(dispute, 'jury') and dispute.jury.assignments.filter(juror=request.user).exists()

    if not is_party and not is_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    jury = getattr(dispute, 'jury', None)
    user_assignment = jury.assignments.filter(juror=request.user).first() if jury else None
    user_vote = jury.votes.filter(juror=request.user).first() if jury else None

    conversation = getattr(task, 'conversation', None)
    chat_messages = conversation.messages.all() if conversation else []

    context = {
        'dispute': dispute,
        'task': task,
        'jury': jury,
        'user_assignment': user_assignment,
        'user_vote': user_vote,
        'conversation': conversation,
        'chat_messages': chat_messages,
        'is_party': is_party,
        'is_juror': is_juror,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if not hasattr(dispute, 'jury'):
        messages.error(request, "No jury found for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    jury = dispute.jury
    check_and_handle_jury_timeouts(jury)

    if dispute.status != 'open' or jury.status != 'voting':
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = JurorAssignment.objects.filter(jury=jury, juror=request.user, status='assigned').first()
    if not assignment:
        messages.error(request, "You are not an active assigned juror for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JuryVote.objects.filter(jury=jury, juror=request.user).exists():
        messages.info(request, "You have already cast your vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a party to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        voted_for_id = int(voted_for_id)
    except ValueError:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    valid_choices = [task.posted_by_id]
    if task.taken_by_id:
        valid_choices.append(task.taken_by_id)

    if voted_for_id not in valid_choices:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = User.objects.get(id=voted_for_id)

    with transaction.atomic():
        JuryVote.objects.create(
            jury=jury,
            juror=request.user,
            voted_for=voted_for_user
        )
        assignment.status = 'voted'
        assignment.save()

        process_jury_consensus(jury)

    messages.success(request, "Your vote has been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


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

            sample_and_assign_jurors(dispute)

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
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        if hasattr(dispute, 'jury'):
            dispute.jury.status = 'concluded'
            dispute.jury.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
