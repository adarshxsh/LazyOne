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
        # If conversation not found, redirect to home with an error
        messages.error(request, "Chat not found.")
        return redirect('home')

    if request.user not in conversation.participants.all():
        logger.warning("Step 2: User is not a participant. Redirecting to home.")
        messages.error(request, "You are not authorized to view this chat.")
        return redirect('home') # Redirect to home page
    logger.info("Step 2: User is a valid participant.")

    try:
        # This is for the Django-based message system, which we are bypassing for Firestore.
        # We will pass an empty list to the template.
        messages_list = [] # Renamed to avoid conflict with django.contrib.messages
        logger.info("Step 3: Bypassing Django message fetching for Firestore.")
    except Exception as e:
        logger.error(f"ERROR at Step 3 (Message Handling): {e}")

    try:
        # Mark related notifications as read
        notification_link = reverse('chat_view', args=[conversation_id])
        updated_count = Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
        logger.info(f"Step 4: Marked {updated_count} related notifications as read.")
    except Exception as e:
        logger.error(f"ERROR at Step 4 (Marking notifications): {e}")

    context = {'conversation': conversation, 'messages': messages_list}
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)
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
            for participant in conversation.participants.all():
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
