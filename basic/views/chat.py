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

    # Check conversation type
    deliberation_dispute = getattr(conversation, 'deliberation_dispute', None)
    if not deliberation_dispute and hasattr(conversation, 'dispute_deliberation'):
        deliberation_dispute = conversation.dispute_deliberation

    is_read_only = False
    is_evidence_portal = False

    if deliberation_dispute:
        task = deliberation_dispute.task
        # Requirement 4: Block task posters and takers from entering or reading jury deliberation channels
        if request.user == task.posted_by or request.user == task.taken_by:
            return HttpResponseForbidden("You are not authorized to access the jury deliberation channel.")
        
        is_assigned_juror = deliberation_dispute.jurors.filter(id=request.user.id).exists()
        if not is_assigned_juror and not request.user.is_staff:
            return HttpResponseForbidden("You are not authorized to access this jury deliberation room.")

    elif conversation.task:
        task = conversation.task
        is_participant = request.user in conversation.participants.all()
        is_assigned_juror = hasattr(task, 'dispute') and task.dispute.jurors.filter(id=request.user.id).exists()
        is_staff = request.user.is_staff

        if not is_participant and not is_assigned_juror and not is_staff:
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

        if not is_participant:
            is_read_only = True
            if is_assigned_juror:
                is_evidence_portal = True

    else:
        # Direct message conversation
        if request.user not in conversation.participants.all() and not request.user.is_staff:
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

    messages_list = list(conversation.messages.all())

    try:
        notification_link = reverse('chat_view', args=[conversation_id])
        Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
    except Exception as e:
        logger.error(f"ERROR at Step 4 (Marking notifications): {e}")

    context = {
        'conversation': conversation,
        'messages': messages_list,
        'is_read_only': is_read_only,
        'is_evidence_portal': is_evidence_portal,
        'is_deliberation': bool(deliberation_dispute),
        'dispute': deliberation_dispute if deliberation_dispute else (conversation.task.dispute if conversation.task and hasattr(conversation.task, 'dispute') else None)
    }
    
    logger.info(f"--- CHAT_VIEW END: Successfully rendering template. ---")
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)

        deliberation_dispute = getattr(conversation, 'deliberation_dispute', None)
        if not deliberation_dispute and hasattr(conversation, 'dispute_deliberation'):
            deliberation_dispute = conversation.dispute_deliberation

        if deliberation_dispute:
            task = deliberation_dispute.task
            if request.user == task.posted_by or request.user == task.taken_by:
                return HttpResponseForbidden("You are not authorized to send messages in the jury deliberation room.")
            
            is_assigned_juror = deliberation_dispute.jurors.filter(id=request.user.id).exists()
            if not is_assigned_juror and not request.user.is_staff:
                return HttpResponseForbidden("You are not authorized to send messages in this jury deliberation room.")

        elif conversation.task:
            if request.user not in conversation.participants.all():
                return HttpResponseForbidden("You are not authorized to send messages in this task chat.")

        else:
            if request.user not in conversation.participants.all():
                return HttpResponseForbidden("You are not authorized to send messages in this chat.")

        content = request.POST.get('content')
        if content:
            msg = Message.objects.create(
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
            return JsonResponse({
                'status': 'success',
                'message': {
                    'id': msg.id,
                    'sender': msg.sender.username,
                    'content': msg.content,
                    'timestamp': msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                }
            })
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
