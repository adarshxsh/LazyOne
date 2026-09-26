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

    is_read_only = False

    if conversation.conversation_type == 'deliberation':
        dispute = conversation.dispute or (conversation.task.dispute if hasattr(conversation.task, 'dispute') else None)
        if dispute and (request.user == dispute.task.posted_by or request.user == dispute.task.taken_by):
            logger.warning("Task participant attempting to access deliberation chat.")
            messages.error(request, "Task participants are not authorized to view private juror deliberation.")
            return HttpResponseForbidden("Task participants are not authorized to view private juror deliberation.")

        if dispute and (dispute.is_juror(request.user) or request.user.is_staff or request.user.is_superuser or request.user in conversation.participants.all()):
            logger.info("User is an impaneled juror or staff for deliberation.")
        else:
            logger.warning("Step 2: User is not authorized for deliberation chat.")
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')
    else:
        if request.user in conversation.participants.all():
            logger.info("Step 2: User is a valid participant.")
        elif conversation.task and hasattr(conversation.task, 'dispute'):
            dispute = conversation.task.dispute
            if dispute.is_juror(request.user) or request.user.is_staff or request.user.is_superuser:
                is_read_only = True
                logger.info("User is an impaneled juror/staff viewing task chat in read-only mode.")
            else:
                logger.warning("Step 2: User is not authorized to view task chat during dispute.")
                messages.error(request, "You are not authorized to view this chat.")
                return redirect('home')
        else:
            logger.warning("Step 2: User is not a participant. Redirecting to home.")
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

    try:
        messages_list = []
        logger.info("Step 3: Bypassing Django message fetching for Firestore.")
    except Exception as e:
        logger.error(f"ERROR at Step 3 (Message Handling): {e}")

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

    context = {'conversation': conversation, 'messages': messages_list, 'is_read_only': is_read_only}
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)

        if conversation.conversation_type == 'deliberation':
            dispute = conversation.dispute or (conversation.task.dispute if hasattr(conversation.task, 'dispute') else None)
            if dispute and (request.user == dispute.task.posted_by or request.user == dispute.task.taken_by):
                return HttpResponseForbidden("Task participants are not authorized to send messages in private juror deliberation.")
            if dispute and not (dispute.is_juror(request.user) or request.user.is_staff or request.user.is_superuser or request.user in conversation.participants.all()):
                return HttpResponseForbidden("You are not authorized to send messages in this chat.")
        else:
            if request.user not in conversation.participants.all():
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

            recipients = set(conversation.participants.all())
            if conversation.conversation_type == 'deliberation' and conversation.dispute:
                recipients.update(conversation.dispute.jurors.all())

            for participant in recipients:
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
