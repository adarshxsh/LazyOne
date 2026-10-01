from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction, IntegrityError
from django.conf import settings
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote


def evaluate_dispute_consensus(dispute, quorum=None):
    """
    Evaluates whether an open dispute has reached voting quorum and simple majority consensus.
    If consensus is reached, resolves the dispute, distributes/refunds escrow bonds,
    and updates task status accordingly.
    """
    if quorum is None:
        quorum = getattr(settings, 'DISPUTE_VOTING_QUORUM', 3)

    if dispute.status != 'open':
        return False

    task = dispute.task
    votes = dispute.votes.all()
    total_votes = votes.count()

    if total_votes < quorum:
        return False

    poster_votes = votes.filter(voted_for=task.posted_by).count()
    taker_votes = votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    if poster_votes == taker_votes:
        # Tie - consensus not reached yet
        return False

    with transaction.atomic():
        # Lock dispute to prevent concurrent duplicate settlement
        dispute_obj = Dispute.objects.select_for_update().get(id=dispute.id)
        if dispute_obj.status != 'open':
            return False

        dispute_obj.status = 'resolved'
        dispute_obj.save()

        dispute.status = 'resolved'

        if poster_votes > taker_votes:
            # Poster wins simple majority
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_refund',
                description=f"Refunded task reward based on majority vote consensus for dispute on task: '{task.title}'"
            )

            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded following majority vote victory on task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Security deposit bond forfeited following majority vote decision on task: '{task.title}'"
                )
            winning_user = task.posted_by
        else:
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
                    description=f"Awarded task reward based on majority vote consensus for dispute on task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()

            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(
                    reason_description=f"Security deposit bond refunded following majority vote victory on task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.taken_by,
                    reason_description=f"Security deposit bond forfeited following majority vote decision on task: '{task.title}'"
                )
            winning_user = task.taken_by

        # Send notifications
        participants = [task.posted_by]
        if task.taken_by and task.taken_by not in participants:
            participants.append(task.taken_by)

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for participant in participants:
            Notification.objects.create(
                recipient=participant,
                message=f"Dispute for task '{task.title}' has been resolved by majority consensus in favor of {winning_user.username}.",
                link=dispute_link
            )

        return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_participant = request.user in [task.posted_by, task.taken_by]
    is_staff = request.user.is_staff

    # Allow access if dispute is open (for community jury review) OR user is participant/staff
    if dispute.status != 'open' and not is_participant and not is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0
    total_votes = dispute.votes.count()
    quorum = getattr(settings, 'DISPUTE_VOTING_QUORUM', 3)

    user_vote = dispute.votes.filter(voter=request.user).first() if request.user.is_authenticated else None
    can_vote = (
        dispute.status == 'open'
        and not is_participant
        and user_vote is None
        and request.user.is_authenticated
    )

    context = {
        'dispute': dispute,
        'task': task,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'total_votes': total_votes,
        'quorum': quorum,
        'user_vote': user_vote,
        'is_participant': is_participant,
        'can_vote': can_vote,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "This dispute is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user in [task.posted_by, task.taken_by]:
        messages.error(request, "Task poster and task taker cannot vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for_id') or request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a side to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    try:
        voted_for_id = int(voted_for_id)
    except (ValueError, TypeError):
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    valid_targets = [task.posted_by_id]
    if task.taken_by_id:
        valid_targets.append(task.taken_by_id)

    if voted_for_id not in valid_targets:
        messages.error(request, "Selected user is not a valid dispute party.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)

    try:
        with transaction.atomic():
            vote, created = DisputeVote.objects.get_or_create(
                dispute=dispute,
                voter=request.user,
                defaults={'voted_for': voted_for_user}
            )
            if not created:
                messages.warning(request, "You have already voted on this dispute.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            messages.success(request, f"Your vote for {voted_for_user.username} has been recorded.")

            # Trigger consensus evaluation upon vote submission
            resolved = evaluate_dispute_consensus(dispute)
            if resolved:
                messages.info(request, "Dispute has reached voting consensus and was automatically resolved!")

    except IntegrityError:
        messages.warning(request, "You have already voted on this dispute.")

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

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
