import math
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, JurorVote


def finalize_appeal_dispute(dispute, winning_user):
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by
    losing_user = taken_by if winning_user == posted_by else posted_by

    with transaction.atomic():
        dispute.status = 'appeal_resolved'
        dispute.resolved_at = timezone.now()
        dispute.save()

        # 1. Task Settlement
        if winning_user == taken_by and taken_by:
            task.status = 'completed'
            task.save()
            taker_profile = taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded for winning dispute appeal on task: '{task.title}'"
            )
        else:
            task.status = 'cancelled'
            task.save()
            poster_profile = posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for winning dispute appeal on task: '{task.title}'"
            )

        # 2. Appeal Fee refund for winning appellant
        if dispute.appellant == winning_user and dispute.appeal_fee > 0:
            appellant_profile = dispute.appellant.userprofile
            appellant_profile.rewards += dispute.appeal_fee
            appellant_profile.save()
            RewardLedger.objects.create(
                user=dispute.appellant,
                task=task,
                amount=dispute.appeal_fee,
                transaction_type='dispute_refund',
                description=f"Appeal fee refunded for winning dispute on task: '{task.title}'"
            )

        # 3. Automated Juror Stake Slashing for Minority / Dissenting Voters
        dissenting_votes = dispute.votes.exclude(voted_for=winning_user)
        penalty_amount = 50
        for vote in dissenting_votes:
            juror = vote.juror
            juror_profile = juror.userprofile
            # Slashing penalties must not exceed the juror's active stake or cause negative total reward balances
            slash_amount = max(0, min(juror_profile.rewards, penalty_amount))
            if slash_amount > 0:
                juror_profile.rewards -= slash_amount
                juror_profile.save()
                RewardLedger.objects.create(
                    user=juror,
                    task=task,
                    amount=-slash_amount,
                    transaction_type='juror_slashing',
                    description=f"Juror stake slashing penalty for dissenting vote on dispute for task: '{task.title}'"
                )

        # 4. Notifications
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(
            recipient=winning_user,
            message=f"The appeal jury ruled in your favor for task '{task.title}'.",
            link=dispute_link
        )
        if losing_user:
            Notification.objects.create(
                recipient=losing_user,
                message=f"The appeal jury ruled against you in dispute for task '{task.title}'.",
                link=dispute_link
            )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_litigant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = request.user in dispute.jurors.all()

    if not is_litigant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = None
    if is_juror:
        user_vote = JurorVote.objects.filter(dispute=dispute, juror=request.user).first()

    assigned_jurors_count = dispute.jurors.count()
    votes_count = dispute.votes.count()
    majority_threshold = (assigned_jurors_count // 2) + 1 if assigned_jurors_count > 0 else 1

    appeal_fee_logs = RewardLedger.objects.filter(task=task, transaction_type='appeal_fee')

    context = {
        'dispute': dispute,
        'task': task,
        'is_litigant': is_litigant,
        'is_juror': is_juror,
        'user_vote': user_vote,
        'assigned_jurors_count': assigned_jurors_count,
        'votes_count': votes_count,
        'majority_threshold': majority_threshold,
        'required_appeal_fee': dispute.required_appeal_fee,
        'is_appeal_window_active': dispute.is_appeal_window_active,
        'appeal_fee_logs': appeal_fee_logs,
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
        dispute.resolved_at = timezone.now()
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
def file_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only active dispute litigants are eligible to file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appeal_window_active:
        messages.error(request, "The appeal window for this dispute is no longer active.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status == 'appealed':
        messages.info(request, "An appeal has already been filed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal_fee = dispute.required_appeal_fee
    user_profile = request.user.userprofile

    if user_profile.rewards < appeal_fee:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {appeal_fee} points as an appeal fee, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= appeal_fee
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-appeal_fee,
            transaction_type='appeal_fee',
            description=f"Escrow appeal fee held for dispute on task: '{task.title}'"
        )

        dispute.status = 'appealed'
        dispute.appellant = request.user
        dispute.appeal_fee = appeal_fee
        dispute.appealed_at = timezone.now()
        dispute.save()

        # Juror selection & neutrality enforcement
        litigant_ids = {task.posted_by.id}
        if task.taken_by:
            litigant_ids.add(task.taken_by.id)

        poster_friends = set(task.posted_by.userprofile.friends.all().values_list('user_id', flat=True))
        taker_friends = set(task.taken_by.userprofile.friends.all().values_list('user_id', flat=True)) if task.taken_by else set()

        excluded_ids = litigant_ids | poster_friends | taker_friends

        eligible_jurors = list(
            User.objects.filter(is_active=True)
            .exclude(id__in=excluded_ids)
            .order_by('?')[:3]
        )
        if eligible_jurors:
            dispute.jurors.set(eligible_jurors)

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        opposing_litigant = task.taken_by if request.user == task.posted_by else task.posted_by
        if opposing_litigant:
            Notification.objects.create(
                recipient=opposing_litigant,
                message=f"{request.user.username} has filed an appeal for the dispute on task '{task.title}'.",
                link=dispute_link
            )

        for juror in eligible_jurors:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been assigned as a peer juror for a dispute appeal on task '{task.title}'.",
                link=dispute_link
            )

    messages.success(request, f"Appeal filed successfully! {appeal_fee} points deducted as appeal fee.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status != 'appealed':
        messages.error(request, "Voting is closed for this dispute appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user not in dispute.jurors.all():
        messages.error(request, "You are not an assigned juror for this dispute appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.info(request, "You have already cast your vote for this dispute appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for_id')
    allowed_ids = [task.posted_by.id]
    if task.taken_by:
        allowed_ids.append(task.taken_by.id)

    if not voted_for_id or int(voted_for_id) not in allowed_ids:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)

    with transaction.atomic():
        JurorVote.objects.create(
            dispute=dispute,
            juror=request.user,
            voted_for=voted_for_user
        )

        total_jurors = dispute.jurors.count()
        majority_threshold = (total_jurors // 2) + 1 if total_jurors > 0 else 1
        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        taker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

        if poster_votes >= majority_threshold:
            finalize_appeal_dispute(dispute, task.posted_by)
        elif taker_votes >= majority_threshold:
            finalize_appeal_dispute(dispute, task.taken_by)
        elif dispute.votes.count() >= total_jurors:
            winning_user = task.posted_by if poster_votes >= taker_votes else task.taken_by
            finalize_appeal_dispute(dispute, winning_user)

    messages.success(request, "Your juror vote has been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)
