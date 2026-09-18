import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile, Friendship, FriendRequest, JurorAssignment
from django.views.decorators.http import require_POST
from django.urls import reverse

def get_excluded_user_ids_for_dispute(dispute):
    excluded_ids = set()

    litigants = []
    if dispute.task.posted_by:
        litigants.append(dispute.task.posted_by)
    if dispute.task.taken_by:
        litigants.append(dispute.task.taken_by)
    if dispute.raised_by and dispute.raised_by not in litigants:
        litigants.append(dispute.raised_by)

    for litigant in litigants:
        excluded_ids.add(litigant.id)

        try:
            profile = litigant.userprofile
            # Forward M2M
            for f_prof in profile.friends.all():
                excluded_ids.add(f_prof.user_id)
            # Reverse M2M
            for f_prof in UserProfile.objects.filter(friends=profile):
                excluded_ids.add(f_prof.user_id)
            # Friendship model (from_user)
            for friendship in Friendship.objects.filter(from_user=profile).select_related('to_user__user'):
                if friendship.to_user and friendship.to_user.user_id:
                    excluded_ids.add(friendship.to_user.user_id)
            # Friendship model (to_user)
            for friendship in Friendship.objects.filter(to_user=profile).select_related('from_user__user'):
                if friendship.from_user and friendship.from_user.user_id:
                    excluded_ids.add(friendship.from_user.user_id)
        except UserProfile.DoesNotExist:
            pass

        # FriendRequest model
        for fr in FriendRequest.objects.filter(from_user=litigant, is_accepted=True):
            excluded_ids.add(fr.to_user_id)
        for fr in FriendRequest.objects.filter(to_user=litigant, is_accepted=True):
            excluded_ids.add(fr.from_user_id)

    return excluded_ids

def select_juror_panel(dispute, panel_size=3, minimum_stake=100):
    excluded_ids = get_excluded_user_ids_for_dispute(dispute)

    already_assigned_ids = set(dispute.juror_assignments.values_list('user_id', flat=True))
    all_excluded_ids = excluded_ids.union(already_assigned_ids)

    candidate_qs = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=minimum_stake
    ).exclude(
        id__in=all_excluded_ids
    ).select_related('userprofile')

    candidate_list = list(candidate_qs)

    if len(candidate_list) < panel_size:
        dispute.status = 'under_review'
        dispute.save()
        raise ValidationError(
            f"Insufficient eligible jurors found ({len(candidate_list)} available, {panel_size} required). Dispute marked for administrator review."
        )

    selected_jurors = random.sample(candidate_list, panel_size)

    assignments = []
    for juror in selected_jurors:
        profile = UserProfile.objects.select_for_update().get(id=juror.userprofile.id)
        profile.rewards -= minimum_stake
        profile.save()

        RewardLedger.objects.create(
            user=juror,
            task=dispute.task,
            amount=-minimum_stake,
            transaction_type='juror_stake_lock',
            description=f"Juror stake locked for dispute on task: '{dispute.task.title}'"
        )

        assignment = JurorAssignment.objects.create(
            dispute=dispute,
            user=juror,
            staked_amount=minimum_stake,
            status='assigned'
        )
        assignments.append(assignment)

        Notification.objects.create(
            recipient=juror,
            message=f"You have been selected as a juror for dispute on task: '{dispute.task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return assignments

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_assigned_juror = dispute.juror_assignments.filter(user=request.user).exists()
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'under_review']:
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

            try:
                sid = transaction.savepoint()
                select_juror_panel(dispute)
                transaction.savepoint_commit(sid)
            except ValidationError as ve:
                transaction.savepoint_rollback(sid)
                dispute.status = 'under_review'
                dispute.save()
                messages.warning(request, f"Dispute raised, but marked for administrator review: {ve}")

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
        dispute.release_juror_stakes(
            reason_description=f"Juror stake released for withdrawn dispute on task: '{task.title}'"
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
