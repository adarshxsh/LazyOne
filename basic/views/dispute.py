from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_poster = (request.user == task.posted_by)
    is_worker = (request.user == task.taken_by)
    is_party = is_poster or is_worker

    poster_vote_count = dispute.votes.filter(voted_for=task.posted_by).count()
    worker_vote_count = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    user_vote = dispute.votes.filter(voter=request.user).first()
    can_vote = (not is_party) and (dispute.status == 'open') and (user_vote is None)

    can_contest = False
    if dispute.status == 'open':
        if is_poster and dispute.poster_escrow_status == 'none':
            can_contest = True
        elif is_worker and dispute.worker_escrow_status == 'none':
            can_contest = True

    context = {
        'dispute': dispute,
        'task': task,
        'is_party': is_party,
        'is_poster': is_poster,
        'is_worker': is_worker,
        'poster_vote_count': poster_vote_count,
        'worker_vote_count': worker_vote_count,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'can_contest': can_contest,
        'juror_stake_amount': Dispute.JUROR_STAKE_AMOUNT,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if request.user != task.taken_by and request.user != task.posted_by:
        messages.error(request, "You are not authorized to raise a dispute for this task.")
        return redirect('my_tasks')

    if task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task that is currently in progress.")
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

            is_worker = (request.user == task.taken_by)

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
            else:
                dispute = Dispute(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            if is_worker:
                dispute.worker_deposit_amount = deposit_amount
                dispute.worker_escrow_status = 'held'
                recipient = task.posted_by
            else:
                dispute.poster_deposit_amount = deposit_amount
                dispute.poster_escrow_status = 'held'
                recipient = task.taken_by

            dispute.save()

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'. Matching deposit bond required to contest.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def contest_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to contest this dispute.")
        return redirect('home')

    is_poster = (request.user == task.posted_by)
    is_worker = (request.user == task.taken_by)

    if (is_poster and dispute.poster_escrow_status == 'held') or (is_worker and dispute.worker_escrow_status == 'held'):
        messages.info(request, "You have already posted your deposit bond for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {deposit_amount} points as a matching deposit bond to contest this dispute, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        if is_poster:
            dispute.poster_deposit_amount = deposit_amount
            dispute.poster_escrow_status = 'held'
            other_party = task.taken_by
        else:
            dispute.worker_deposit_amount = deposit_amount
            dispute.worker_escrow_status = 'held'
            other_party = task.posted_by

        dispute.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type='dispute_deposit',
            description=f"Matching security deposit bond held for contesting dispute on task: '{task.title}'"
        )

        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"{request.user.username} has posted a matching deposit bond to contest the dispute on '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Matching deposit bond of {deposit_amount} points posted successfully. Dispute is now fully contested.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Disputing parties cannot vote as community jurors.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_role = request.POST.get('voted_for') # 'poster' or 'worker'
    if voted_for_role == 'poster':
        voted_for = task.posted_by
    elif voted_for_role == 'worker' and task.taken_by:
        voted_for = task.taken_by
    else:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    stake_amount = Dispute.JUROR_STAKE_AMOUNT
    user_profile = request.user.userprofile
    if user_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {stake_amount} points to vote as a juror, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= stake_amount
        user_profile.save()

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for,
            stake_amount=stake_amount
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror stake held for vote on dispute for task: '{task.title}'"
        )

    messages.success(request, f"Vote submitted successfully! {stake_amount} points staked.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def settle_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, status='open')
    task = dispute.task

    if not request.user.is_staff and request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "You are not authorized to settle this dispute.")
        return redirect('home')

    winner_choice = request.POST.get('winner') # 'poster', 'worker', or user ID
    if winner_choice == 'poster':
        winner = task.posted_by
    elif winner_choice == 'worker':
        winner = task.taken_by
    elif winner_choice and winner_choice.isdigit():
        winner = get_object_or_404(User, id=int(winner_choice))
    else:
        messages.error(request, "Invalid winner specified.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    dispute.settle_dispute(winner=winner, resolved_by_admin=request.user.is_staff)
    messages.success(request, f"Dispute settled successfully in favor of {winner.username}.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user, status='open')
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        other_party = task.posted_by if request.user == task.taken_by else task.taken_by
        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')

