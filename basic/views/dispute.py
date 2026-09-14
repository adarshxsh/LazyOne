from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.db import transaction
from django.db.models import Q
from django.core.paginator import Paginator

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
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
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')
        dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
        task.status = 'disputed'
        task.save()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    task.status = 'in_progress'
    task.save()
    dispute.delete()
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
        link=reverse('my_tasks')
    )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def staff_dispute_manage(request):
    if not request.user.is_staff:
        messages.error(request, "You are not authorized to access staff dispute management.")
        return redirect('home')
    
    status_filter = request.GET.get('status', 'all')
    search_query = (request.GET.get('search') or request.GET.get('q', '')).strip()

    disputes = Dispute.objects.select_related('task', 'raised_by', 'task__posted_by', 'task__taken_by').order_by('-created_at')

    if status_filter in ['open', 'resolved']:
        disputes = disputes.filter(status=status_filter)

    if search_query:
        disputes = disputes.filter(
            Q(task__title__icontains=search_query) |
            Q(task__posted_by__username__icontains=search_query) |
            Q(task__taken_by__username__icontains=search_query) |
            Q(raised_by__username__icontains=search_query)
        )

    paginator = Paginator(disputes, 10)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'disputes': page_obj,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'search_query': search_query,
    }
    return render(request, 'disputes_manage.html', context)

@login_required(login_url='/login/')
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        messages.error(request, "You are not authorized to perform staff dispute actions.")
        return redirect('home')
    
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if dispute.status == 'resolved':
        messages.error(request, "This dispute has already been resolved.")
        return redirect('staff_dispute_manage')

    resolution_type = request.POST.get('action') or request.POST.get('mode') or request.POST.get('resolution_type')
    note = (request.POST.get('note') or request.POST.get('resolution_note') or '').strip()

    if resolution_type not in ['award_taker', 'refund_poster', 'dismiss']:
        messages.error(request, "Invalid resolution mode selected.")
        return redirect('staff_dispute_manage')

    note_text = f" Moderator note: {note}" if note else ""

    with transaction.atomic():
        if resolution_type == 'award_taker':
            taker = task.taken_by
            if not taker:
                messages.error(request, "Cannot award taker: Task has no assigned taker.")
                return redirect('staff_dispute_manage')
            
            taker_profile = taker.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=taker,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Staff resolved dispute in favor of taker for task: '{task.title}'."
            )

            task.status = 'completed'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff moderator resolved the dispute for task '{task.title}' in favor of taker ({taker.username}).{note_text}",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            Notification.objects.create(
                recipient=taker,
                message=f"Staff moderator resolved the dispute for task '{task.title}' in your favor. {task.reward} points awarded.{note_text}",
                link=reverse('dispute_detail', args=[dispute.id])
            )

            messages.success(request, f"Dispute resolved. {task.reward} points awarded to {taker.username}.")

        elif resolution_type == 'refund_poster':
            poster = task.posted_by
            poster_profile = poster.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=poster,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Staff resolved dispute with refund for task: '{task.title}'."
            )

            task.status = 'cancelled'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=poster,
                message=f"Staff moderator resolved the dispute for task '{task.title}' with a refund. {task.reward} points returned.{note_text}",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff moderator resolved the dispute for task '{task.title}' with a refund to poster ({poster.username}).{note_text}",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, f"Dispute resolved. {task.reward} points refunded to {poster.username}.")

        elif resolution_type == 'dismiss':
            task.status = 'in_progress'
            task.save()

            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff moderator dismissed the dispute for task '{task.title}'. The task remains in progress.{note_text}",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff moderator dismissed the dispute for task '{task.title}'. The task remains in progress.{note_text}",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

            messages.success(request, f"Dispute dismissed for task '{task.title}'.")

    return redirect('staff_dispute_manage')

