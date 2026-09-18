from functools import wraps
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from datetime import datetime
from ..models import Dispute, Task, RewardLedger, Notification, UserProfile

def staff_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            messages.error(request, "Access denied. Staff privileges required.")
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return _wrapped_view

@staff_required
def staff_dispute_dashboard(request):
    disputes = Dispute.objects.select_related('task', 'raised_by', 'task__posted_by', 'task__taken_by').order_by('-created_at')

    # Status filter
    status_filter = request.GET.get('status', 'all')
    if status_filter in ['open', 'resolved']:
        disputes = disputes.filter(status=status_filter)

    # Keyword search
    query = request.GET.get('q', '').strip()
    if query:
        disputes = disputes.filter(
            Q(task__title__icontains=query) |
            Q(task__description__icontains=query) |
            Q(reason__icontains=query) |
            Q(raised_by__username__icontains=query) |
            Q(task__posted_by__username__icontains=query) |
            Q(task__taken_by__username__icontains=query)
        )

    # Date filter
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str, '%Y-%m-%d')
            disputes = disputes.filter(created_at__gte=timezone.make_aware(date_from))
        except ValueError:
            pass
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str, '%Y-%m-%d')
            date_to_end = datetime.combine(date_to.date(), datetime.max.time())
            disputes = disputes.filter(created_at__lte=timezone.make_aware(date_to_end))
        except ValueError:
            pass

    # Aggregates / Stats
    total_disputes = Dispute.objects.count()
    open_disputes = Dispute.objects.filter(status='open').count()
    resolved_disputes = Dispute.objects.filter(status='resolved').count()

    context = {
        'disputes': disputes,
        'status_filter': status_filter,
        'query': query,
        'date_from': date_from_str,
        'date_to': date_to_str,
        'total_disputes': total_disputes,
        'open_disputes': open_disputes,
        'resolved_disputes': resolved_disputes,
    }
    return render(request, 'moderation_dashboard.html', context)

@staff_required
@require_POST
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('staff_dispute_dashboard')

    ruling = request.POST.get('ruling')
    task = dispute.task

    if ruling == 'poster':
        with transaction.atomic():
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'cancelled'
            task.save()

            poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Dispute refund (moderator ruling favoring poster) for task: '{task.title}'"
            )

            dispute_url = reverse('dispute_detail', args=[dispute.id])

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor. Reserved points ({task.reward}) refunded.",
                link=dispute_url
            )

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in favor of the poster.",
                    link=dispute_url
                )

        messages.success(request, f"Dispute resolved in favor of task poster ({task.posted_by.username}). Task cancelled and points refunded.")

    elif ruling == 'taker':
        if not task.taken_by:
            messages.error(request, "Cannot rule in favor of taker as no user took this task.")
            return redirect('staff_dispute_dashboard')

        with transaction.atomic():
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'completed'
            task.save()

            taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Dispute payout (moderator ruling favoring taker) for task: '{task.title}'"
            )

            dispute_url = reverse('dispute_detail', args=[dispute.id])

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor. Reward points ({task.reward}) awarded.",
                link=dispute_url
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of the taker.",
                link=dispute_url
            )

        messages.success(request, f"Dispute resolved in favor of task taker ({task.taken_by.username}). Task marked complete and points awarded.")

    else:
        messages.error(request, "Invalid resolution ruling specified.")

    redirect_to = request.POST.get('next') or reverse('staff_dispute_dashboard')
    return redirect(redirect_to)
