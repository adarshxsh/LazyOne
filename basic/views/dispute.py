from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, Appeal, AppealJuror
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    
    is_litigant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = AppealJuror.objects.filter(appeal__dispute=dispute, juror=request.user).exists()

    if not is_litigant and not is_juror and not request.user.is_staff and dispute.status != 'under_appeal':
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    active_appeal = dispute.appeals.filter(status='pending').first() or dispute.appeals.last()

    juror_assignment = None
    if active_appeal and request.user.is_authenticated:
        juror_assignment = AppealJuror.objects.filter(appeal=active_appeal, juror=request.user).first()

    context = {
        'dispute': dispute,
        'task': task,
        'active_appeal': active_appeal,
        'is_litigant': is_litigant,
        'is_juror': is_juror,
        'juror_assignment': juror_assignment,
        'is_appealable': dispute.is_appealable and is_litigant,
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
        messages.error(request, "Only task participants can file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'resolved':
        messages.error(request, "Only resolved disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    resolved_time = dispute.resolved_at or dispute.created_at
    if timezone.now() > resolved_time + timedelta(hours=72):
        messages.error(request, "The 72-hour appeal escalation SLA window has expired for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.appeals.filter(status='pending').exists():
        messages.error(request, "An appeal is already currently active for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {deposit_amount} points as an appeal deposit bond, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    round_number = dispute.appeals.count() + 1

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        appeal = Appeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            appeal_deposit=deposit_amount,
            round_number=round_number,
            status='pending',
            ruling='pending',
            quorum=3,
            consensus_threshold=0.66
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type='appeal_deposit',
            description=f"Appeal deposit bond held for Round {round_number} dispute on task: '{task.title}'"
        )

        dispute.status = 'under_appeal'
        dispute.save()

        task.status = 'under_appeal'
        task.save()

        excluded_ids = [task.posted_by.id, request.user.id]
        if task.taken_by:
            excluded_ids.append(task.taken_by.id)

        eligible_users = list(User.objects.exclude(id__in=excluded_ids).filter(is_active=True).order_by('?')[:3])
        for juror_user in eligible_users:
            AppealJuror.objects.create(appeal=appeal, juror=juror_user)
            Notification.objects.create(
                recipient=juror_user,
                message=f"You have been assigned as a peer juror for an appeal on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        opposing_user = task.taken_by if request.user == task.posted_by else task.posted_by
        if opposing_user:
            Notification.objects.create(
                recipient=opposing_user,
                message=f"{request.user.username} has escalated task '{task.title}' to a peer juror appeal (Round {round_number}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appeal Round {round_number} filed successfully. {deposit_amount} points held as appeal deposit bond.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, appeal_id):
    appeal = get_object_or_404(Appeal, id=appeal_id)
    dispute = appeal.dispute
    task = dispute.task

    if appeal.status != 'pending':
        messages.error(request, "This appeal is no longer open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user == task.posted_by or (task.taken_by and request.user == task.taken_by) or request.user == dispute.raised_by:
        messages.error(request, "Litigants and task participants are not allowed to serve as peer jurors or cast votes on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror_assignment = AppealJuror.objects.filter(appeal=appeal, juror=request.user).first()
    if not juror_assignment:
        current_assigned_count = appeal.juror_assignments.count()
        if current_assigned_count < appeal.quorum:
            juror_assignment = AppealJuror.objects.create(appeal=appeal, juror=request.user)
        else:
            messages.error(request, "You are not an assigned peer juror for this appeal.")
            return redirect('dispute_detail', dispute_id=dispute.id)

    if juror_assignment.vote != 'pending':
        messages.error(request, "You have already submitted your verdict vote for this appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        juror_assignment.vote = vote_choice
        juror_assignment.voted_at = timezone.now()
        juror_assignment.save()

        voted_jurors = appeal.juror_assignments.filter(vote__in=['poster', 'taker'])
        total_votes = voted_jurors.count()
        poster_votes = voted_jurors.filter(vote='poster').count()
        taker_votes = voted_jurors.filter(vote='taker').count()

        poster_ratio = poster_votes / total_votes if total_votes > 0 else 0
        taker_ratio = taker_votes / total_votes if total_votes > 0 else 0

        winning_choice = None
        if total_votes >= appeal.quorum:
            if poster_ratio >= appeal.consensus_threshold:
                winning_choice = 'poster'
            elif taker_ratio >= appeal.consensus_threshold:
                winning_choice = 'taker'

        if winning_choice:
            winner_user = task.posted_by if winning_choice == 'poster' else task.taken_by
            loser_user = task.taken_by if winning_choice == 'poster' else task.posted_by

            appellant_won = (appeal.appellant == winner_user)

            if appellant_won:
                appeal.ruling = 'upheld'
                appellant_profile = appeal.appellant.userprofile
                appellant_profile.rewards += appeal.appeal_deposit
                appellant_profile.save()
                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=appeal.appeal_deposit,
                    transaction_type='appeal_refund',
                    description=f"Appeal deposit bond refunded after successful appeal on task: '{task.title}'"
                )

                if winning_choice == 'taker':
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded task reward following upheld appeal on task: '{task.title}'"
                        )
                    task.status = 'completed'
                else:
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refunded task reward following upheld appeal on task: '{task.title}'"
                    )
                    task.status = 'cancelled'

                if dispute.escrow_status == 'held':
                    if dispute.raised_by == winner_user:
                        dispute.refund_deposit(
                            reason_description=f"Initial deposit bond refunded after winning appeal on task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=winner_user,
                            reason_description=f"Deposit bond slashed due to overturned appeal on task: '{task.title}'"
                        )
                RewardLedger.objects.create(
                    user=loser_user,
                    task=task,
                    amount=0,
                    transaction_type='appeal_slash',
                    description=f"Bad-actor litigant deposit bond slashed following appeal on task: '{task.title}'"
                )

            else:
                appeal.ruling = 'overturned'
                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=0,
                    transaction_type='appeal_slash',
                    description=f"Appeal deposit bond forfeited/slashed for failed appeal on task: '{task.title}'"
                )

                if winning_choice == 'taker':
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded task reward following rejected appeal on task: '{task.title}'"
                        )
                    task.status = 'completed'
                else:
                    poster_profile = task.posted_by.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=task.posted_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Refunded task reward following rejected appeal on task: '{task.title}'"
                    )
                    task.status = 'cancelled'

                if dispute.escrow_status == 'held':
                    if dispute.raised_by == winner_user:
                        dispute.refund_deposit(
                            reason_description=f"Initial dispute deposit bond settled for task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=winner_user,
                            reason_description=f"Initial dispute deposit bond forfeited for task: '{task.title}'"
                        )

            for juror_item in voted_jurors:
                if juror_item.vote == winning_choice:
                    j_prof = juror_item.juror.userprofile
                    j_prof.rewards += 25
                    j_prof.save()
                    RewardLedger.objects.create(
                        user=juror_item.juror,
                        task=task,
                        amount=25,
                        transaction_type='juror_reward',
                        description=f"Reward payout for honest peer juror consensus vote on task: '{task.title}'"
                    )
                    juror_item.is_rewarded = True
                    juror_item.save()
                else:
                    j_prof = juror_item.juror.userprofile
                    slash_amount = max(0, min(25, j_prof.rewards))
                    j_prof.rewards -= slash_amount
                    j_prof.save()
                    RewardLedger.objects.create(
                        user=juror_item.juror,
                        task=task,
                        amount=-slash_amount,
                        transaction_type='juror_slash',
                        description=f"Points slashed for dishonest minority juror vote on appeal for task: '{task.title}'"
                    )
                    juror_item.is_slashed = True
                    juror_item.save()

            appeal.status = 'resolved'
            appeal.resolved_at = timezone.now()
            appeal.save()

            dispute.status = 'resolved'
            dispute.resolved_at = timezone.now()
            dispute.save()

            task.save()

            ruling_text = "upheld in favor of appellant" if appellant_won else "rejected in favor of appellee"
            Notification.objects.create(
                recipient=appeal.appellant,
                message=f"Appeal for task '{task.title}' has been resolved ({ruling_text}).",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            opposing_litigant = task.taken_by if appeal.appellant == task.posted_by else task.posted_by
            if opposing_litigant:
                Notification.objects.create(
                    recipient=opposing_litigant,
                    message=f"Appeal for task '{task.title}' has been resolved ({ruling_text}).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            for juror_item in voted_jurors:
                Notification.objects.create(
                    recipient=juror_item.juror,
                    message=f"The appeal for task '{task.title}' you voted on has reached consensus. Rewards/penalties finalized.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        else:
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"A peer juror has submitted a vote on the appeal for task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"A peer juror has submitted a vote on the appeal for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    messages.success(request, "Your verdict vote has been recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

