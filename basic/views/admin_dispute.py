from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.http import HttpResponseForbidden
from ..models import Dispute, Task, Conversation, RewardLedger, Notification


@staff_member_required(login_url='/login/')
def admin_dispute_list(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Access denied: Staff members only.")

    disputes = Dispute.objects.select_related(
        'task', 'task__posted_by', 'task__taken_by', 'raised_by'
    ).all().order_by('-created_at')

    status_filter = request.GET.get('status', '').strip()
    if status_filter in ['open', 'resolved']:
        disputes = disputes.filter(status=status_filter)

    escrow_filter = request.GET.get('escrow_status', '').strip()
    if escrow_filter in ['held', 'refunded', 'forfeited']:
        disputes = disputes.filter(escrow_status=escrow_filter)

    search_query = request.GET.get('q', '').strip()
    if search_query:
        disputes = disputes.filter(
            Q(task__title__icontains=search_query) |
            Q(task__description__icontains=search_query) |
            Q(reason__icontains=search_query) |
            Q(raised_by__username__icontains=search_query) |
            Q(task__posted_by__username__icontains=search_query) |
            Q(task__taken_by__username__icontains=search_query)
        )

    context = {
        'disputes': disputes,
        'status_filter': status_filter,
        'escrow_filter': escrow_filter,
        'search_query': search_query,
    }
    return render(request, 'admin_dispute_list.html', context)


@staff_member_required(login_url='/login/')
def admin_dispute_detail(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Access denied: Staff members only.")

    dispute = get_object_or_404(
        Dispute.objects.select_related('task', 'task__posted_by', 'task__taken_by', 'raised_by'),
        id=dispute_id
    )
    task = dispute.task

    conversation = Conversation.objects.filter(task=task).first()
    messages_list = conversation.messages.select_related('sender').all() if conversation else []

    participants_q = Q(task=task)
    if task.posted_by:
        participants_q |= Q(user=task.posted_by)
    if task.taken_by:
        participants_q |= Q(user=task.taken_by)

    ledger_entries = RewardLedger.objects.filter(participants_q).select_related('user', 'task').order_by('-created_at')

    context = {
        'dispute': dispute,
        'task': task,
        'conversation': conversation,
        'messages_list': messages_list,
        'ledger_entries': ledger_entries,
    }
    return render(request, 'admin_dispute_detail.html', context)


@staff_member_required(login_url='/login/')
@require_POST
def admin_dispute_resolve(request, dispute_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Access denied: Staff members only.")

    dispute = get_object_or_404(
        Dispute.objects.select_related('task', 'task__posted_by', 'task__taken_by', 'raised_by'),
        id=dispute_id
    )
    task = dispute.task

    resolution_action = request.POST.get('resolution_action') or request.POST.get('action') or 'favor_worker'
    admin_note = request.POST.get('admin_note') or request.POST.get('reason') or ''

    with transaction.atomic():
        if resolution_action in ['favor_worker', 'worker', 'rule_for_worker']:
            if task.taken_by and task.status != 'completed':
                worker_profile = task.taken_by.userprofile
                worker_profile.rewards += task.reward
                worker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward awarded by staff resolution for task: '{task.title}'"
                )
                task.status = 'completed'
                task.save()

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(
                        reason_description=admin_note or f"Security deposit bond refunded to worker by staff resolution for task: '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=admin_note or f"Security deposit bond forfeited to worker by staff resolution for task: '{task.title}'"
                    )

            dispute.status = 'resolved'
            dispute.save()

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff resolved dispute for '{task.title}' in your favor.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff resolved dispute for '{task.title}' in favor of the worker.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            messages.success(request, f"Dispute resolved in favor of worker ({task.taken_by.username if task.taken_by else 'N/A'}).")

        elif resolution_action in ['favor_poster', 'poster', 'rule_for_poster']:
            if task.status in ['in_progress', 'disputed', 'available']:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Reward refunded by staff resolution for task: '{task.title}'"
                )
                task.status = 'cancelled'
                task.save()

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(
                        reason_description=admin_note or f"Security deposit bond refunded to poster by staff resolution for task: '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=admin_note or f"Security deposit bond forfeited to poster by staff resolution for task: '{task.title}'"
                    )

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff resolved dispute for '{task.title}' in your favor.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff resolved dispute for '{task.title}' in favor of the poster.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            messages.success(request, f"Dispute resolved in favor of poster ({task.posted_by.username}).")

        elif resolution_action in ['split', 'refund_deposit', 'cancel', 'refund_worker_cancel']:
            if task.status in ['in_progress', 'disputed', 'available']:
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Reward refunded by staff split resolution for task: '{task.title}'"
                )
                task.status = 'cancelled'
                task.save()

            if dispute.escrow_status == 'held':
                dispute.refund_deposit(
                    reason_description=admin_note or f"Security deposit bond refunded by staff resolution for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff resolved dispute for '{task.title}': task cancelled and points refunded.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff resolved dispute for '{task.title}': deposit bond refunded and task cancelled.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            messages.success(request, "Dispute resolved with split/cancel resolution.")

        elif resolution_action == 'forfeit_deposit':
            if dispute.escrow_status == 'held':
                beneficiary = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by
                dispute.forfeit_deposit(
                    beneficiary=beneficiary,
                    reason_description=admin_note or f"Security deposit bond forfeited by staff resolution for task: '{task.title}'"
                )

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff updated dispute status for '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff updated dispute status for '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            messages.success(request, "Deposit bond forfeited and dispute resolved.")

        else:
            dispute.status = 'resolved'
            dispute.save()
            messages.success(request, "Dispute status updated to resolved.")

    return redirect('admin_dispute_detail', dispute_id=dispute.id)
