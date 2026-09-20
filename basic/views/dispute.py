import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JurorAssignment, UserProfile, Friendship
from django.views.decorators.http import require_POST
from django.urls import reverse

MIN_JUROR_REWARD_BALANCE = 100
JUROR_STAKE_BOND = 20

def select_neutral_jurors_for_dispute(dispute):
    task = dispute.task

    counterparty_user_ids = set()
    if task.posted_by_id:
        counterparty_user_ids.add(task.posted_by_id)
    if task.taken_by_id:
        counterparty_user_ids.add(task.taken_by_id)

    excluded_user_ids = set(counterparty_user_ids)

    # Exclude direct friends via UserProfile.friends
    for user_id in counterparty_user_ids:
        try:
            profile = UserProfile.objects.get(user_id=user_id)
            friend_user_ids = profile.friends.all().values_list('user_id', flat=True)
            excluded_user_ids.update(friend_user_ids)
        except UserProfile.DoesNotExist:
            pass

    # Exclude direct friends via Friendship model
    counterparty_profiles = UserProfile.objects.filter(user_id__in=counterparty_user_ids)
    friendships = Friendship.objects.filter(
        Q(from_user__in=counterparty_profiles) | Q(to_user__in=counterparty_profiles)
    ).select_related('from_user', 'to_user')

    for fs in friendships:
        if fs.from_user and fs.from_user.user_id:
            excluded_user_ids.add(fs.from_user.user_id)
        if fs.to_user and fs.to_user.user_id:
            excluded_user_ids.add(fs.to_user.user_id)

    # Exclude existing jurors
    existing_juror_ids = dispute.juror_assignments.values_list('juror_id', flat=True)
    excluded_user_ids.update(existing_juror_ids)

    # Candidate jurors must have rewards >= 100 and not be excluded
    candidates = list(
        User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=MIN_JUROR_REWARD_BALANCE
        ).exclude(id__in=excluded_user_ids)
    )

    if len(candidates) < 3:
        dispute.status = 'admin_review'
        dispute.requires_admin_review = True
        dispute.save()
        return []

    target_count = min(5, len(candidates))
    selected_jurors = random.sample(candidates, target_count)

    assigned_jurors = []
    for juror in selected_jurors:
        profile = juror.userprofile
        profile.rewards -= JUROR_STAKE_BOND
        profile.save()

        assignment = JurorAssignment.objects.create(
            dispute=dispute,
            juror=juror,
            stake_amount=JUROR_STAKE_BOND
        )

        RewardLedger.objects.create(
            user=juror,
            task=task,
            amount=-JUROR_STAKE_BOND,
            transaction_type='juror_stake',
            description=f"Juror stake bond held for dispute on task: '{task.title}'"
        )

        Notification.objects.create(
            recipient=juror,
            message=f"You have been assigned as a juror for dispute on task: '{task.title}'. A stake bond of {JUROR_STAKE_BOND} points has been locked.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        assigned_jurors.append(assignment)

    dispute.status = 'open'
    dispute.requires_admin_review = False
    dispute.save()
    return assigned_jurors


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_counterparty = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = dispute.is_assigned_juror(request.user)

    if not is_counterparty and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    juror_assignment = None
    has_voted = False
    if is_juror:
        juror_assignment = dispute.juror_assignments.filter(juror=request.user).first()
        if juror_assignment and juror_assignment.voted_for:
            has_voted = True

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'has_voted': has_voted,
        'juror_assignment': juror_assignment,
        'juror_assignments': dispute.juror_assignments.all(),
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'admin_review']:
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
                dispute.requires_admin_review = False
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

            # Trigger dynamic juror selection
            jurors = select_neutral_jurors_for_dispute(dispute)

        if len(jurors) >= 3:
            messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. {len(jurors)} neutral jurors assigned.")
        else:
            messages.warning(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Fewer than 3 neutral jurors were available; flagged for administrative review.")

        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if not dispute.is_assigned_juror(request.user):
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for voting.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    assignment = dispute.juror_assignments.get(juror=request.user)
    if assignment.voted_for:
        messages.error(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id or int(voted_for_id) not in [task.posted_by_id, task.taken_by_id]:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)

    with transaction.atomic():
        assignment.voted_for = voted_for_user
        assignment.voted_at = timezone.now()
        assignment.save()

        # Check if all assigned jurors have voted
        total_jurors = dispute.juror_assignments.count()
        voted_jurors = dispute.juror_assignments.filter(voted_for__isnull=False).count()

        if total_jurors > 0 and voted_jurors == total_jurors:
            poster_votes = dispute.juror_assignments.filter(voted_for=task.posted_by).count()
            taker_votes = dispute.juror_assignments.filter(voted_for=task.taken_by).count()

            winner = task.posted_by if poster_votes >= taker_votes else task.taken_by

            majority_assignments = dispute.juror_assignments.filter(voted_for=winner)
            for ja in majority_assignments:
                jprofile = ja.juror.userprofile
                reward_payout = ja.stake_amount + 10
                jprofile.rewards += reward_payout
                jprofile.save()

                RewardLedger.objects.create(
                    user=ja.juror,
                    task=task,
                    amount=reward_payout,
                    transaction_type='juror_reward',
                    description=f"Juror reward payout for voting with majority on dispute for task: '{task.title}'"
                )

            if winner == dispute.raised_by:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded for resolved dispute on task: '{task.title}'")
            else:
                dispute.forfeit_deposit(beneficiary=winner, reason_description=f"Security deposit bond forfeited to {winner.username} for resolved dispute on task: '{task.title}'")

            dispute.status = 'resolved'
            dispute.save()

            if winner == task.taken_by:
                task.status = 'completed'
            else:
                task.status = 'in_progress'
            task.save()

    messages.success(request, "Your vote has been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        # Refund juror stakes if any
        for ja in dispute.juror_assignments.all():
            jprofile = ja.juror.userprofile
            jprofile.rewards += ja.stake_amount
            jprofile.save()
            RewardLedger.objects.create(
                user=ja.juror,
                task=task,
                amount=ja.stake_amount,
                transaction_type='juror_reward',
                description=f"Juror stake bond refunded as dispute was withdrawn on task: '{task.title}'"
            )

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
