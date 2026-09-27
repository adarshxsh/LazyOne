from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from ..models import Conversation, Message, Notification
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages # Import messages
import logging

logger = logging.getLogger(__name__)

@login_required(login_url='/login/')
def chat_view(request, conversation_id):
    logger.info(f"--- CHAT_VIEW START: conv_id={conversation_id}, user={request.user.username} ---")
    
    try:
        conversation = get_object_or_404(Conversation, id=conversation_id)
        logger.info("Step 1: Conversation object found.")
    except Exception as e:
        logger.error(f"FATAL ERROR at Step 1 (get_object_or_404): {e}")
        messages.error(request, "Chat not found.")
        return redirect('home')

    task = conversation.task
    dispute = conversation.dispute or (task.dispute if task and hasattr(task, 'dispute') else None)

    is_deliberation = (conversation.conversation_type == 'deliberation')
    is_juror = dispute and dispute.jurors.filter(id=request.user.id).exists()
    is_participant = (task and (request.user == task.posted_by or request.user == task.taken_by)) or (request.user in conversation.participants.all())
    is_staff = request.user.is_staff

    is_read_only = False

    if is_deliberation:
        # Task participants (poster and taker) are strictly forbidden from juror deliberation channels
        if task and (request.user == task.posted_by or request.user == task.taken_by):
            messages.error(request, "Task participants are not authorized to view juror deliberation messages.")
            return redirect('home')
        if not is_juror and not is_staff and request.user not in conversation.participants.all():
            messages.error(request, "You are not authorized to view this deliberation chat.")
            return redirect('home')
    else:
        # Main task conversation or DM
        if is_participant or is_staff:
            is_read_only = False
        elif is_juror:
            # Jurors get read-only access to main task conversation
            is_read_only = True
        else:
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

    try:
        messages_list = conversation.messages.all()
    except Exception as e:
        logger.error(f"ERROR at Step 3 (Message Handling): {e}")
        messages_list = []

    try:
        notification_link = reverse('chat_view', args=[conversation_id])
        updated_count = Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
        logger.info(f"Step 4: Marked {updated_count} related notifications as read.")
    except Exception as e:
        logger.error(f"ERROR at Step 4 (Marking notifications): {e}")

    context = {
        'conversation': conversation,
        'messages': messages_list,
        'is_read_only': is_read_only,
        'is_deliberation': is_deliberation,
    }
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)
        task = conversation.task
        dispute = conversation.dispute or (task.dispute if task and hasattr(task, 'dispute') else None)

        is_deliberation = (conversation.conversation_type == 'deliberation')
        is_juror = dispute and dispute.jurors.filter(id=request.user.id).exists()
        is_participant = (task and (request.user == task.posted_by or request.user == task.taken_by)) or (request.user in conversation.participants.all())
        is_staff = request.user.is_staff

        if is_deliberation:
            if task and (request.user == task.posted_by or request.user == task.taken_by):
                return HttpResponseForbidden("Task participants are not authorized to send messages in juror deliberation channels.")
            if not is_juror and not is_staff and request.user not in conversation.participants.all():
                return HttpResponseForbidden("You are not authorized to send messages in this deliberation chat.")
        else:
            if is_juror and not is_participant and not is_staff:
                return HttpResponseForbidden("Jurors have read-only access to main task conversations.")
            if not is_participant and not is_staff:
                return HttpResponseForbidden("You are not authorized to send messages in this chat.")

        content = request.POST.get('content')
        if content:
            Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=content
            )
            conversation.last_message_at = timezone.now()
            conversation.save()

            notify_users = set(conversation.participants.all())
            if is_deliberation and dispute:
                notify_users.update(dispute.jurors.all())
            if task:
                notify_users.add(task.posted_by)
                if task.taken_by:
                    notify_users.add(task.taken_by)

            for participant in notify_users:
                if participant != request.user:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"New message from {request.user.username}",
                        link=reverse('chat_view', args=[conversation_id])
                    )
            return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'}, status=400)

@login_required(login_url='/login/')
def start_chat(request, user_id):
    other_user = get_object_or_404(User, id=user_id)
    conversation = Conversation.objects.filter(
        participants=request.user
    ).filter(
        participants=other_user
    ).filter(
        task__isnull=True
    ).first()

    if not conversation:
        conversation = Conversation.objects.create()
        conversation.participants.add(request.user, other_user)

    return redirect('chat_view', conversation_id=conversation.id)
