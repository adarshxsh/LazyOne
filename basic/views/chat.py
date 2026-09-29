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

    is_participant = request.user in conversation.participants.all()
    is_staff = request.user.is_staff
    is_read_only = False
    is_authorized = False

    if conversation.is_deliberation or conversation.dispute:
        dispute = conversation.dispute
        is_juror = dispute.jurors.filter(id=request.user.id).exists() if dispute else False
        if is_participant or is_staff or is_juror:
            is_authorized = True
            if dispute and dispute.status != 'open':
                is_read_only = True
    elif conversation.task:
        task = conversation.task
        is_task_participant = request.user in [task.posted_by, task.taken_by]
        dispute = getattr(task, 'dispute', None)
        is_juror = dispute.jurors.filter(id=request.user.id).exists() if dispute else False
        if is_participant or is_task_participant or is_staff or is_juror:
            is_authorized = True
            if is_juror and not is_participant and not is_task_participant and not is_staff:
                is_read_only = True
    else:
        if is_participant or is_staff:
            is_authorized = True

    if not is_authorized:
        logger.warning(f"Step 2: User {request.user.username} is not authorized to view conversation {conversation_id}.")
        return HttpResponseForbidden("You are not authorized to view this chat.")

    logger.info("Step 2: User is authorized to view chat.")

    try:
        messages_list = list(conversation.messages.select_related('sender').all())
        logger.info("Step 3: Fetched conversation messages.")
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
    }
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)
        is_participant = request.user in conversation.participants.all()
        is_staff = request.user.is_staff

        if conversation.is_deliberation or conversation.dispute:
            dispute = conversation.dispute
            if not dispute:
                return HttpResponseForbidden("Invalid deliberation channel.")
            
            is_juror = dispute.jurors.filter(id=request.user.id).exists()
            if not (is_juror or is_staff or is_participant):
                return HttpResponseForbidden("You are not authorized to send messages in this deliberation channel.")

            if dispute.status != 'open':
                return HttpResponseForbidden("Deliberation channel is closed.")

        elif conversation.task:
            task = conversation.task
            is_task_participant = request.user in [task.posted_by, task.taken_by]
            dispute = getattr(task, 'dispute', None)
            is_juror = dispute.jurors.filter(id=request.user.id).exists() if dispute else False

            if is_juror and not is_participant and not is_task_participant and not is_staff:
                return HttpResponseForbidden("Jurors have read-only access to task chat.")

            if not (is_participant or is_task_participant or is_staff):
                return HttpResponseForbidden("You are not authorized to send messages in this chat.")
        else:
            if not (is_participant or is_staff):
                return HttpResponseForbidden("You are not authorized to send messages in this chat.")

        content = request.POST.get('content')
        if content:
            message_obj = Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=content
            )
            conversation.last_message_at = timezone.now()
            conversation.save()

            recipients = set(conversation.participants.all())
            if conversation.task:
                if conversation.task.posted_by:
                    recipients.add(conversation.task.posted_by)
                if conversation.task.taken_by:
                    recipients.add(conversation.task.taken_by)
            if conversation.dispute:
                recipients.update(conversation.dispute.jurors.all())

            for recipient in recipients:
                if recipient != request.user:
                    Notification.objects.create(
                        recipient=recipient,
                        message=f"New message from {request.user.username}",
                        link=reverse('chat_view', args=[conversation_id])
                    )

            next_url = request.POST.get('next')
            if next_url:
                return redirect(next_url)
            return JsonResponse({'status': 'success', 'message_id': message_obj.id})
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
