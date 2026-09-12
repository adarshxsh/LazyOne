from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute, Task, Notification
from django.views.decorators.http import require_POST
from django.urls import reverse
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

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

        notification_msg = f"{request.user.username} has raised a dispute for your task: '{task.title}'."
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(
            recipient=task.posted_by,
            message=notification_msg,
            link=dispute_link
        )

        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                payload = {
                    'type': 'dispute_created',
                    'event': 'dispute_created',
                    'dispute_id': dispute.id,
                    'task_id': task.id,
                    'task_title': task.title,
                    'raised_by': request.user.username,
                    'reason': dispute.reason,
                    'status': dispute.status,
                    'status_display': dispute.get_status_display(),
                    'message': notification_msg,
                    'link': dispute_link
                }
                async_to_sync(channel_layer.group_send)(
                    f"dispute_{dispute.id}",
                    {'type': 'dispute_update', 'data': payload}
                )
                async_to_sync(channel_layer.group_send)(
                    f"user_{task.posted_by.id}",
                    {'type': 'dispute_notification', 'data': payload}
                )
        except Exception:
            pass

        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    dispute_id_val = dispute.id
    task_id_val = task.id
    posted_by_id = task.posted_by.id
    taken_by_id = task.taken_by.id if task.taken_by else None

    task.status = 'in_progress'
    task.save()
    dispute.delete()

    notification_msg = f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress."
    my_tasks_link = reverse('my_tasks')
    Notification.objects.create(
        recipient=task.posted_by,
        message=notification_msg,
        link=my_tasks_link
    )

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            payload = {
                'type': 'dispute_withdrawn',
                'event': 'dispute_withdrawn',
                'dispute_id': dispute_id_val,
                'task_id': task_id_val,
                'task_title': task.title,
                'withdrawn_by': request.user.username,
                'status': 'withdrawn',
                'status_display': 'Withdrawn',
                'task_status': 'in_progress',
                'task_status_display': 'In Progress',
                'message': notification_msg,
                'link': my_tasks_link
            }
            async_to_sync(channel_layer.group_send)(
                f"dispute_{dispute_id_val}",
                {'type': 'dispute_update', 'data': payload}
            )
            for u_id in set([posted_by_id, taken_by_id]):
                if u_id:
                    async_to_sync(channel_layer.group_send)(
                        f"user_{u_id}",
                        {'type': 'dispute_notification', 'data': payload}
                    )
    except Exception:
        pass

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

