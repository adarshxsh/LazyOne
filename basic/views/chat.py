from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from ..models import Conversation, Message, Notification
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages # Import messages
import logging
from ..services.permissions import ChatService
from ..services import messaging
from ..services.marketplace import TransitionError
from ..realtime import create_custom_token
from ..ratelimit import ratelimit

logger = logging.getLogger(__name__)

@login_required(login_url='/login/')
def chat_view(request, public_id):
    logger.info(f"--- CHAT_VIEW START: public_id={public_id}, user={request.user.username} ---")
    
    try:
        conversation = get_object_or_404(Conversation, public_id=public_id)
        logger.info("Step 1: Conversation object found.")
    except Exception as e:
        logger.error(f"FATAL ERROR at Step 1 (get_object_or_404): {e}")
        # If conversation not found, redirect to home with an error
        messages.error(request, "Chat not found.")
        return redirect('home')

    if not ChatService.can_read(request.user, conversation):
        logger.warning("Step 2: User is not authorized. Redirecting to home.")
        messages.error(request, "You are not authorized to view this chat.")
        return redirect('home') # Redirect to home page
    logger.info("Step 2: User is authorized to read.")

    try:
        # This is for the Django-based message system, which we are bypassing for Firestore.
        # We will pass an empty list to the template.
        messages_list = [] # Renamed to avoid conflict with django.contrib.messages
        logger.info("Step 3: Bypassing Django message fetching for Firestore.")
    except Exception as e:
        logger.error(f"ERROR at Step 3 (Message Handling): {e}")

    try:
        # Mark related notifications as read
        notification_link = reverse('chat_view', args=[public_id])
        updated_count = Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
        logger.info(f"Step 4: Marked {updated_count} related notifications as read.")
    except Exception as e:
        logger.error(f"ERROR at Step 4 (Marking notifications): {e}")

    context = {'conversation': conversation, 'messages': messages_list,
               'can_send': ChatService.can_send(request.user, conversation)}
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
@require_POST
@ratelimit('send_message', '60/minute')
def send_message(request, public_id):
    conversation = get_object_or_404(Conversation, public_id=public_id)
    try:
        msg = messaging.send(conversation, request.user, request.POST.get('content'))
    except PermissionError as e:
        return HttpResponseForbidden(str(e))
    except TransitionError as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=400)
    return JsonResponse({'status': 'success', 'message': messaging.serialize(msg)})


@login_required(login_url='/login/')
def message_history(request, public_id):
    """JSON history; also the polling fallback when realtime is unavailable."""
    conversation = get_object_or_404(Conversation, public_id=public_id)
    if not ChatService.can_read(request.user, conversation):
        return HttpResponseForbidden("Not allowed.")
    try:
        after = int(request.GET.get('after') or 0) or None
    except ValueError:
        after = None
    return JsonResponse({'messages': messaging.history(conversation, after_id=after)})


@login_required(login_url='/login/')
def realtime_token(request):
    """Firebase custom token for read-only Firestore access (see firestore.rules)."""
    token = create_custom_token(request.user)
    if token is None:
        return JsonResponse({'token': None}, status=503)
    return JsonResponse({'token': token})


@login_required(login_url='/login/')
def start_chat(request, user_id):
    other_user = get_object_or_404(User, id=user_id)
    if other_user == request.user:
        messages.error(request, "You cannot start a chat with yourself.")
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

    return redirect('chat_view', public_id=conversation.public_id)
