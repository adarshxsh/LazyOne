from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from ..models import Dispute, Task, Notification, RewardLedger, DisputeMessage
from django.views.decorators.http import require_POST
from django.urls import reverse

def is_assigned_juror(user, dispute):
    if not user or not user.is_authenticated:
        return False
    if hasattr(dispute, 'is_assigned_juror'):
        try:
            if dispute.is_assigned_juror(user):
                return True
        except Exception:
            pass
    if hasattr(dispute, 'jurors'):
        try:
            if dispute.jurors.filter(id=user.id).exists():
                return True
        except Exception:
            pass
    if hasattr(dispute, 'assigned_jurors'):
        try:
            if dispute.assigned_jurors.filter(id=user.id).exists():
                return True
        except Exception:
            pass
    try:
        from ..models import JuryAssignment
        if JuryAssignment.objects.filter(dispute=dispute, juror=user).exists():
            return True
    except Exception:
        pass
    try:
        from ..models import Jury
        if Jury.objects.filter(dispute=dispute, juror=user).exists():
            return True
    except Exception:
        pass
    return False

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(
        Dispute.objects.select_related(
            'task', 'task__posted_by', 'task__taken_by', 'raised_by'
        ).prefetch_related(
            'jurors',
            'messages',
            'messages__sender',
            'task__conversation',
            'task__conversation__messages',
            'task__conversation__messages__sender'
        ),
        id=dispute_id
    )
    task = dispute.task
    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = is_assigned_juror(request.user, dispute)
    is_staff = request.user.is_staff

    if not is_participant and not is_juror and not is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    task_conversation = getattr(task, 'conversation', None)
    task_messages = []
    if task_conversation:
        task_messages = task_conversation.messages.select_related('sender').all()

    deliberation_messages = dispute.messages.select_related('sender').all()

    context = {
        'dispute': dispute,
        'task': task,
        'task_conversation': task_conversation,
        'task_messages': task_messages,
        'deliberation_messages': deliberation_messages,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'can_post': is_participant or is_juror or is_staff,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def post_dispute_message(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_participant = request.user in [task.posted_by, task.taken_by]
    is_juror = is_assigned_juror(request.user, dispute)
    is_staff = request.user.is_staff

    if not is_participant and not is_juror and not is_staff:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return HttpResponseForbidden("You are not authorized to post in this dispute deliberation channel.")
        messages.error(request, "You are not authorized to post in this dispute deliberation channel.")
        return HttpResponseForbidden("You are not authorized to post in this dispute deliberation channel.")

    content = request.POST.get('content') or request.POST.get('message')
    if not content and request.content_type == 'application/json':
        import json
        try:
            data = json.loads(request.body)
            content = data.get('content') or data.get('message')
        except Exception:
            pass

    if content and content.strip():
        DisputeMessage.objects.create(
            dispute=dispute,
            sender=request.user,
            content=content.strip()
        )

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'status': 'success'})

        messages.success(request, "Deliberation message posted.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({'status': 'error', 'message': 'Message content cannot be empty'}, status=400)

    messages.error(request, "Message content cannot be empty.")
    return redirect('dispute_detail', dispute_id=dispute.id)

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
