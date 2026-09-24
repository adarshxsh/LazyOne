from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from ..models import Conversation, Message, Notification, Dispute, DisputeJuror
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

    deliberation_dispute = getattr(conversation, 'dispute_deliberation', None) or Dispute.objects.filter(deliberation_conversation=conversation).first()
    is_read_only = False

    if deliberation_dispute:
        # Task poster and worker are strictly forbidden from juror deliberation channel
        if request.user == deliberation_dispute.task.posted_by or request.user == deliberation_dispute.task.taken_by:
            logger.warning("User is task poster or worker attempting to access deliberation chat. Denied.")
            return HttpResponseForbidden("Task poster and task doer are strictly forbidden from viewing or participating in juror deliberation.")

        is_juror = DisputeJuror.objects.filter(dispute=deliberation_dispute, user=request.user).exists()
        if not is_juror and not request.user.is_staff and request.user not in conversation.participants.all():
            messages.error(request, "You are not authorized to view this deliberation chat.")
            return redirect('home')
        logger.info("User authorized for deliberation chat.")
    else:
        if request.user in conversation.participants.all():
            logger.info("Step 2: User is a valid participant.")
            is_read_only = False
        elif conversation.task and hasattr(conversation.task, 'dispute') and (DisputeJuror.objects.filter(dispute=conversation.task.dispute, user=request.user).exists() or request.user.is_staff):
            logger.info("Step 2: User is an assigned juror/staff viewing primary task evidence in read-only mode.")
            is_read_only = True
        else:
            logger.warning("Step 2: User is not authorized to view this chat.")
            messages.error(request, "You are not authorized to view this chat.")
            return redirect('home')

    try:
        messages_list = []
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
        deliberation_dispute = getattr(conversation, 'dispute_deliberation', None) or Dispute.objects.filter(deliberation_conversation=conversation).first()

        if deliberation_dispute:
            if request.user == deliberation_dispute.task.posted_by or request.user == deliberation_dispute.task.taken_by:
                return HttpResponseForbidden("Task poster and task doer are strictly forbidden from participating in juror deliberation.")
            is_juror = DisputeJuror.objects.filter(dispute=deliberation_dispute, user=request.user).exists()
            if not is_juror and not request.user.is_staff and request.user not in conversation.participants.all():
                return HttpResponseForbidden("You are not authorized to send messages in this deliberation chat.")
        else:
            if conversation.task and hasattr(conversation.task, 'dispute'):
                is_juror = DisputeJuror.objects.filter(dispute=conversation.task.dispute, user=request.user).exists()
                if is_juror and request.user not in [conversation.task.posted_by, conversation.task.taken_by]:
                    return HttpResponseForbidden("Jurors cannot post messages in the primary task conversation.")
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
