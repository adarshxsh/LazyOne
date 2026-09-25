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
    is_deliberation = False

    # 1. Deliberation Channel Check
    if hasattr(conversation, 'deliberation_dispute') and conversation.deliberation_dispute is not None:
        is_deliberation = True
        dispute = conversation.deliberation_dispute
        task = dispute.task
        is_juror = dispute.jurors.filter(id=request.user.id).exists()
        is_staff = request.user.is_staff
        is_participant = (request.user == task.posted_by or request.user == task.taken_by)

        if dispute.status == 'open':
            # Task participants are excluded from deliberation channel while open
            if is_participant:
                messages.error(request, "You are not authorized to view the juror deliberation channel.")
                return redirect('home')
            
            # Restricted to assigned jurors and staff
            if not is_juror and not is_staff:
                messages.error(request, "You are not authorized to view this deliberation channel.")
                return redirect('home')
            
            is_read_only = False
        else:
            if not is_juror and not is_staff and not is_participant:
                messages.error(request, "You are not authorized to view this deliberation channel.")
                return redirect('home')
            is_read_only = True

    # 2. Main Task Conversation Check
    elif conversation.task:
        task = conversation.task
        if task.status == 'disputed':
            # Disputed task transcript: read-only viewing access granted for jurors, staff, and community reviewers
            is_read_only = True
        else:
            # Non-disputed task: strict participant-only access guard
            if request.user not in conversation.participants.all():
                logger.warning("User is not a participant in non-disputed task chat. Redirecting to home.")
                messages.error(request, "You are not authorized to view this chat.")
                return redirect('home')

    # 3. Direct Conversation Check
    else:
        if request.user not in conversation.participants.all():
            logger.warning("User is not a participant in direct chat. Redirecting to home.")
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

    messages_list = []

    try:
        # Mark related notifications as read
        notification_link = reverse('chat_view', args=[conversation_id])
        updated_count = Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
        logger.info(f"Marked {updated_count} related notifications as read.")
    except Exception as e:
        logger.error(f"ERROR marking notifications: {e}")

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

        # 1. Deliberation Channel
        if hasattr(conversation, 'deliberation_dispute') and conversation.deliberation_dispute is not None:
            dispute = conversation.deliberation_dispute
            if dispute.status != 'open':
                return HttpResponseForbidden("Deliberation channel is closed.")
            
            is_juror = dispute.jurors.filter(id=request.user.id).exists()
            if not is_juror and not request.user.is_staff:
                return HttpResponseForbidden("Only assigned jurors and staff can post messages in the deliberation channel.")
            
            content = request.POST.get('content')
            if content:
                Message.objects.create(
                    conversation=conversation,
                    sender=request.user,
                    content=content
                )
                conversation.last_message_at = timezone.now()
                conversation.save()

                for juror in dispute.jurors.all():
                    if juror != request.user:
                        Notification.objects.create(
                            recipient=juror,
                            message=f"New deliberation note from {request.user.username}",
                            link=reverse('chat_view', args=[conversation_id])
                        )
                return JsonResponse({'status': 'success'})

        # 2. Main Task Conversation
        elif conversation.task:
            if conversation.task.status == 'disputed':
                return HttpResponseForbidden("Messaging is disabled for disputed tasks.")
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

        # 3. Direct Conversation
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
