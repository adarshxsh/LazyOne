from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger, DisputeAppeal, UserProfile
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.utils import timezone

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    appeal = getattr(dispute, 'appeal', None)
    is_participant = (request.user == task.posted_by or request.user == task.taken_by)

    can_arbitrate = request.user.is_staff and dispute.status == 'open'
    can_appeal = is_participant and dispute.is_appealable
    can_review_appeal = request.user.is_staff and dispute.status == 'appealed'

    context = {
        'dispute': dispute,
        'task': task,
        'appeal': appeal,
        'is_participant': is_participant,
        'can_arbitrate': can_arbitrate,
        'can_appeal': can_appeal,
        'can_review_appeal': can_review_appeal,
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
    if dispute.status != 'open':
        messages.error(request, "Only open disputes can be withdrawn.")
        return redirect('dispute_detail', dispute_id=dispute.id)
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
def arbitrate_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can arbitrate disputes.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute is not open for arbitration.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    outcome = request.POST.get('outcome')
    rationale = request.POST.get('rationale') or request.POST.get('justification')

    if outcome not in ['favour_poster', 'favour_taker', 'favor_poster', 'favor_taker'] or not rationale:
        messages.error(request, "Outcome and decision rationale are required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    normalized_outcome = 'favour_poster' if outcome in ['favour_poster', 'favor_poster'] else 'favour_taker'
    task = dispute.task

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolution_outcome = normalized_outcome
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.resolution_rationale = rationale
        dispute.save()

        if normalized_outcome == 'favour_poster':
            poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Refund from arbitration on task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()
            winner = task.posted_by
        else:
            taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
            taker_profile.rewards += task.reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_resolution',
                description=f"Reward payout from arbitration on task: '{task.title}'"
            )
            task.status = 'completed'
            task.save()
            winner = task.taken_by

        for participant in [task.posted_by, task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Dispute for task '{task.title}' resolved in favor of {winner.username}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute arbitrated successfully in favor of {winner.username}.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def submit_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only direct task participants can submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_appealable:
        messages.error(request, "This dispute cannot be appealed at this time.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason') or request.POST.get('justification')
    if not reason:
        messages.error(request, "Justification is required to submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        DisputeAppeal.objects.create(
            dispute=dispute,
            appealed_by=request.user,
            reason=reason,
            status='pending'
        )
        dispute.status = 'appealed'
        dispute.save()

        for participant in [task.posted_by, task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"An appeal has been submitted for dispute on task '{task.title}' by {request.user.username}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, "Appeal submitted successfully and routed for senior staff review.")

    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def review_appeal(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "Only authorized staff members can review dispute appeals.")
        return redirect('dispute_detail', dispute_id=dispute_id)

    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'appealed' or not hasattr(dispute, 'appeal'):
        messages.error(request, "This dispute does not have an active appeal to review.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    decision = request.POST.get('decision')
    review_notes = request.POST.get('review_notes') or request.POST.get('justification') or request.POST.get('notes') or ''

    if decision not in ['uphold', 'overturn']:
        messages.error(request, "A valid decision (uphold or overturn) is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    appeal = dispute.appeal
    task = dispute.task

    with transaction.atomic():
        appeal.reviewed_by = request.user
        appeal.reviewed_at = timezone.now()
        appeal.review_notes = review_notes

        if decision == 'uphold':
            appeal.status = 'upheld'
            dispute.status = 'closed'
            appeal.save()
            dispute.save()
            msg = f"The initial arbitration decision for '{task.title}' was upheld."
        else:
            appeal.status = 'overturned'
            dispute.status = 'closed'
            appeal.save()

            initial_outcome = dispute.resolution_outcome
            if initial_outcome == 'favour_poster':
                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards -= task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=-task.reward,
                    transaction_type='appeal_overturn',
                    description=f"Appeal overturn reversal for task: '{task.title}'"
                )

                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='appeal_overturn',
                    description=f"Appeal overturn award for task: '{task.title}'"
                )

                dispute.resolution_outcome = 'favour_taker'
                task.status = 'completed'
                new_winner = task.taken_by
            else:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards -= task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=-task.reward,
                    transaction_type='appeal_overturn',
                    description=f"Appeal overturn reversal for task: '{task.title}'"
                )

                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='appeal_overturn',
                    description=f"Appeal overturn refund for task: '{task.title}'"
                )

                dispute.resolution_outcome = 'favour_poster'
                task.status = 'cancelled'
                new_winner = task.posted_by

            dispute.save()
            task.save()
            msg = f"The initial decision for '{task.title}' was overturned in favor of {new_winner.username}."

        for participant in [task.posted_by, task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Appeal review completed for task '{task.title}': Decision {decision.capitalize()}.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, msg)

    return redirect('dispute_detail', dispute_id=dispute.id)
