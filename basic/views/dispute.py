from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.db.models import Q

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
        priority = request.POST.get('priority', 'medium')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        if priority not in ['low', 'medium', 'high']:
            priority = 'medium'

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
                dispute.priority = priority
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    priority=priority,
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


@user_passes_test(lambda u: getattr(u, 'is_staff', False) or getattr(u, 'is_superuser', False), login_url='/login/')
def admin_dispute_dashboard(request):
    status_filter = request.GET.get('status', 'all')
    priority_filter = request.GET.get('priority', 'all')
    search_query = request.GET.get('q', '').strip()

    disputes = Dispute.objects.all().select_related('task', 'task__posted_by', 'task__taken_by', 'raised_by').order_by('-created_at')

    if status_filter and status_filter != 'all':
        disputes = disputes.filter(status=status_filter)

    if priority_filter and priority_filter != 'all':
        disputes = disputes.filter(priority=priority_filter)

    if search_query:
        disputes = disputes.filter(
            Q(task__title__icontains=search_query) |
            Q(task__posted_by__username__icontains=search_query) |
            Q(task__taken_by__username__icontains=search_query) |
            Q(raised_by__username__icontains=search_query) |
            Q(reason__icontains=search_query)
        )

    total_disputes_count = Dispute.objects.count()
    open_disputes_count = Dispute.objects.filter(status='open').count()
    resolved_disputes_count = Dispute.objects.filter(status='resolved').count()
    high_priority_count = Dispute.objects.filter(priority='high').count()

    context = {
        'disputes': disputes,
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'search_query': search_query,
        'total_disputes_count': total_disputes_count,
        'open_disputes_count': open_disputes_count,
        'resolved_disputes_count': resolved_disputes_count,
        'high_priority_count': high_priority_count,
    }
    return render(request, 'admin_dispute_dashboard.html', context)


@user_passes_test(lambda u: getattr(u, 'is_staff', False) or getattr(u, 'is_superuser', False), login_url='/login/')
@require_POST
def admin_override_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    action = request.POST.get('action')
    notes = request.POST.get('notes', '').strip()

    acting_staff_id = request.user.id
    staff_note = f"[Staff User ID: {acting_staff_id}]"
    if notes:
        staff_note = f"{notes} {staff_note}"

    with transaction.atomic():
        if action in ['poster_wins', 'force_settle_poster', 'payout_poster']:
            # Poster wins: refund reward to poster, task cancelled, dispute resolved
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_settlement_poster',
                description=f"Admin override resolution (Poster Wins) for task '{task.title}'. {staff_note}"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff (ID: {acting_staff_id}) resolved dispute for '{task.title}' in your favor. {task.reward} points refunded.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff (ID: {acting_staff_id}) resolved dispute for '{task.title}' in favor of the poster.",
                    link=reverse('my_tasks')
                )

            messages.success(request, f"Dispute #{dispute.id} force-resolved: Poster ({task.posted_by.username}) won. {task.reward} points refunded.")

        elif action in ['taker_wins', 'force_settle_taker', 'payout_taker']:
            # Taker wins: payout reward to taker, task completed, dispute resolved
            if not task.taken_by:
                messages.error(request, "Cannot award payout to taker because task has no assigned taker.")
                return redirect('admin_dispute_dashboard')

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            task.status = 'completed'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='dispute_settlement_taker',
                description=f"Admin override resolution (Taker Wins) for task '{task.title}'. {staff_note}"
            )

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Staff (ID: {acting_staff_id}) resolved dispute for '{task.title}' in your favor. {task.reward} points awarded.",
                link=reverse('my_tasks')
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff (ID: {acting_staff_id}) resolved dispute for '{task.title}' in favor of {task.taken_by.username}.",
                link=reverse('my_tasks')
            )

            messages.success(request, f"Dispute #{dispute.id} force-resolved: Taker ({task.taken_by.username}) won. {task.reward} points awarded.")

        elif action in ['dismiss', 'force_withdraw', 'dismiss_dispute']:
            # Dismiss dispute: mark dispute resolved (or withdrawn), restore task status
            dispute.status = 'resolved'
            dispute.save()

            if task.taken_by:
                task.status = 'in_progress'
            else:
                task.status = 'available'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=0,
                transaction_type='dispute_dismissal',
                description=f"Admin override action (Dispute Dismissed) for task '{task.title}'. {staff_note}"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff (ID: {acting_staff_id}) dismissed the dispute for '{task.title}'. Task status restored to '{task.status}'.",
                link=reverse('my_tasks')
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff (ID: {acting_staff_id}) dismissed the dispute for '{task.title}'. Task status restored to '{task.status}'.",
                    link=reverse('my_tasks')
                )

            messages.success(request, f"Dispute #{dispute.id} dismissed. Task restored to '{task.status}'.")

        elif action in ['cancel_task', 'cancel']:
            # Cancel task & refund poster
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Admin override action (Task Cancelled) for task '{task.title}'. {staff_note}"
            )

            messages.success(request, f"Task '{task.title}' cancelled and points refunded to poster.")

        else:
            messages.error(request, f"Unknown override action: '{action}'.")

    return redirect('admin_dispute_dashboard')

