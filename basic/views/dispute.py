from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger


def staff_or_superuser_required(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not (request.user.is_staff or request.user.is_superuser):
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


@user_passes_test(staff_or_superuser_required, login_url='/login/')
def admin_disputes_view(request):
    status_filter = request.GET.get('status', 'all')
    search_query = request.GET.get('q', '').strip()

    disputes = Dispute.objects.select_related('task', 'raised_by', 'task__posted_by', 'task__taken_by').order_by('-created_at')

    if status_filter in ['open', 'resolved']:
        disputes = disputes.filter(status=status_filter)

    if search_query:
        disputes = disputes.filter(
            Q(task__title__icontains=search_query) |
            Q(raised_by__username__icontains=search_query) |
            Q(reason__icontains=search_query) |
            Q(task__posted_by__username__icontains=search_query) |
            Q(task__taken_by__username__icontains=search_query)
        )

    paginator = Paginator(disputes, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'disputes': page_obj,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'search_query': search_query,
    }
    return render(request, 'admin_disputes.html', context)


@user_passes_test(staff_or_superuser_required, login_url='/login/')
def admin_dispute_override_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.method == 'POST':
        action = request.POST.get('action') or request.POST.get('resolution')
        deposit_action = request.POST.get('deposit_action', 'auto')
        moderation_note = request.POST.get('moderation_note', '').strip()

        if action not in ['resolve_taker', 'award_taker', 'taker', 'resolve_poster', 'award_poster', 'poster', 'cancel']:
            messages.error(request, "Invalid moderation action selected.")
            return redirect('admin_dispute_override', dispute_id=dispute.id)

        with transaction.atomic():
            note_suffix = f" (Note: {moderation_note})" if moderation_note else ""

            if action in ['resolve_taker', 'award_taker', 'taker']:
                # Award task reward to taker
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Reward awarded by admin moderation override for task: '{task.title}'{note_suffix}"
                    )

                task.status = 'completed'

                # Deposit bond handling
                if deposit_action == 'refund':
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded by admin moderation override on task: '{task.title}'"
                    )
                elif deposit_action == 'forfeit' or deposit_action == 'forfeit_taker':
                    dispute.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=f"Security deposit bond forfeited to taker by admin moderation override on task: '{task.title}'"
                    )
                elif deposit_action == 'forfeit_poster':
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Security deposit bond forfeited to poster by admin moderation override on task: '{task.title}'"
                    )
                else:  # auto
                    if dispute.raised_by == task.taken_by:
                        dispute.refund_deposit(
                            reason_description=f"Security deposit bond refunded by admin moderation override on task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=task.taken_by,
                            reason_description=f"Security deposit bond forfeited to taker by admin moderation override on task: '{task.title}'"
                        )

                resolution_text = "awarded to task taker (completed)"

            else:  # resolve_poster, award_poster, poster, cancel
                # Refund task reward to poster
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Task reward refunded by admin moderation override for task: '{task.title}'{note_suffix}"
                )

                task.status = 'cancelled'

                # Deposit bond handling
                if deposit_action == 'refund':
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded by admin moderation override on task: '{task.title}'"
                    )
                elif deposit_action == 'forfeit' or deposit_action == 'forfeit_poster':
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Security deposit bond forfeited to poster by admin moderation override on task: '{task.title}'"
                    )
                elif deposit_action == 'forfeit_taker':
                    dispute.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=f"Security deposit bond forfeited to taker by admin moderation override on task: '{task.title}'"
                    )
                else:  # auto
                    if dispute.raised_by == task.posted_by:
                        dispute.refund_deposit(
                            reason_description=f"Security deposit bond refunded by admin moderation override on task: '{task.title}'"
                        )
                    else:
                        dispute.forfeit_deposit(
                            beneficiary=task.posted_by,
                            reason_description=f"Security deposit bond forfeited to poster by admin moderation override on task: '{task.title}'"
                        )

                resolution_text = "awarded to task poster (cancelled)"

            dispute.status = 'resolved'
            dispute.save()
            task.save()

            # Notifications
            notify_link = reverse('dispute_detail', args=[dispute.id])
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Admin moderation resolved the dispute for task '{task.title}': Outcome {resolution_text}.",
                link=notify_link
            )

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Admin moderation resolved the dispute for task '{task.title}': Outcome {resolution_text}.",
                    link=notify_link
                )

            messages.success(request, f"Moderation override successfully executed. Outcome: {resolution_text}.")
            return redirect('admin_disputes')

    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'admin_dispute_override.html', context)


resolve_dispute = admin_dispute_override_view
