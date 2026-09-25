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

    is_participant = (request.user == task.posted_by or request.user == task.taken_by)
    has_voted = False
    user_vote = None

    if request.user.is_authenticated and not is_participant:
        vote_obj = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first()
        if vote_obj:
            has_voted = True
            user_vote = vote_obj.vote

    poster_votes = dispute.votes.filter(vote='poster').count()
    taker_votes = dispute.votes.filter(vote='taker').count()

    context = {
        'dispute': dispute,
        'task': task,
        'is_participant': is_participant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
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

@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'open':
        messages.error(request, "Voting is closed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task participants cannot vote on their own task dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            vote=vote_choice
        )
        messages.success(request, f"Your vote in favor of {vote_choice.capitalize()} has been recorded.")

    return redirect('dispute_detail', dispute_id=dispute.id)

def process_dispute_resolution(dispute, winner=None):
    with transaction.atomic():
        if dispute.status == 'resolved':
            return

        task = dispute.task
        poster_votes = dispute.votes.filter(vote='poster').count()
        taker_votes = dispute.votes.filter(vote='taker').count()

        if not winner:
            if poster_votes > taker_votes:
                winner = 'poster'
            elif taker_votes > poster_votes:
                winner = 'taker'
            else:
                if dispute.raised_by == task.posted_by:
                    winner = 'poster'
                else:
                    winner = 'taker'

        if winner == 'poster':
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for resolved dispute on task: '{task.title}'"
            )

            majority_votes = dispute.votes.filter(vote='poster')
            majority_jurors = [v.voter for v in majority_votes]

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.forfeit_deposit(jurors=majority_jurors, reason_description=f"Security deposit bond forfeited for dispute on task: '{task.title}'")
                else:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
            else:
                if task.taken_by:
                    losing_bond = task.deposit_bond_amount
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards -= losing_bond
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=-losing_bond,
                        transaction_type='dispute_forfeit',
                        description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'"
                    )
                    if majority_jurors:
                        share = losing_bond // len(majority_jurors)
                        for juror in majority_jurors:
                            jp = juror.userprofile
                            jp.rewards += share
                            jp.save()
                            RewardLedger.objects.create(
                                user=juror,
                                task=task,
                                amount=share,
                                transaction_type='juror_reward',
                                description=f"Juror reward payout for dispute on task: '{task.title}'"
                            )

        elif winner == 'taker':
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward for resolved dispute on task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()

            majority_votes = dispute.votes.filter(vote='taker')
            majority_jurors = [v.voter for v in majority_votes]

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded for resolved dispute on task: '{task.title}'")
                    losing_bond = task.deposit_bond_amount
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards -= losing_bond
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=-losing_bond,
                        transaction_type='dispute_forfeit',
                        description=f"Deposit bond forfeited for losing dispute on task: '{task.title}'"
                    )
                    if majority_jurors:
                        share = losing_bond // len(majority_jurors)
                        for juror in majority_jurors:
                            jp = juror.userprofile
                            jp.rewards += share
                            jp.save()
                            RewardLedger.objects.create(
                                user=juror,
                                task=task,
                                amount=share,
                                transaction_type='juror_reward',
                                description=f"Juror reward payout for dispute on task: '{task.title}'"
                            )
                else:
                    dispute.forfeit_deposit(jurors=majority_jurors, reason_description=f"Security deposit bond forfeited for dispute on task: '{task.title}'")

        dispute.status = 'resolved'
        dispute.save()

        participants = [task.posted_by]
        if task.taken_by and task.taken_by not in participants:
            participants.append(task.taken_by)

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for participant in participants:
            Notification.objects.create(
                recipient=participant,
                message=f"Dispute for task '{task.title}' has been resolved in favor of the {winner}.",
                link=dispute_link
            )

@login_required(login_url='/login/')
@require_POST
def resolve_dispute_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "Dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    winner = request.POST.get('winner')
    if winner and winner not in ['poster', 'taker']:
        messages.error(request, "Invalid winner selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    process_dispute_resolution(dispute, winner=winner)
    messages.success(request, f"Dispute on task '{dispute.task.title}' has been resolved.")
    return redirect('dispute_detail', dispute_id=dispute.id)
