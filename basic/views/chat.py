from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from ..models import Conversation, Message, Notification, JurorAssignment
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages # Import messages
import logging

logger = logging.getLogger(__name__)

def are_co_jurors_on_active_dispute(user1, user2):
    if not user1 or not user2 or user1 == user2:
        return False
    active_dispute_ids = JurorAssignment.objects.filter(
        juror=user1,
        dispute__status='open'
    ).values_list('dispute_id', flat=True)

    if not active_dispute_ids:
        return False

    return JurorAssignment.objects.filter(
        juror=user2,
        dispute_id__in=active_dispute_ids
    ).exists()


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

    if request.user not in conversation.participants.all():
        logger.warning("Step 2: User is not a participant. Redirecting to home.")
        messages.error(request, "You are not authorized to view this chat.")
        return redirect('home')

    if conversation.task is None:
        for p in conversation.participants.all():
            if p != request.user and are_co_jurors_on_active_dispute(request.user, p):
                messages.error(request, "Direct communication between assigned jurors regarding an active dispute is explicitly blocked.")
                return redirect('home')

    logger.info("Step 2: User is a valid participant.")

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

    context = {'conversation': conversation, 'messages': messages_list}
    
    logger.info("--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)
        if request.user not in conversation.participants.all():
            return HttpResponseForbidden("You are not authorized to send messages in this chat.")
        
        if conversation.task is None:
            for p in conversation.participants.all():
                if p != request.user and are_co_jurors_on_active_dispute(request.user, p):
                    return JsonResponse({
                        'status': 'error',
                        'message': 'Direct communication between assigned jurors regarding an active dispute is explicitly blocked.'
                    }, status=403)

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
    if are_co_jurors_on_active_dispute(request.user, other_user):
        messages.error(request, "Direct communication between assigned jurors regarding an active dispute is explicitly blocked.")
        return redirect('home')

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
