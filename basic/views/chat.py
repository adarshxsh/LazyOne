from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from ..models import Conversation, Message, Notification, Dispute
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages
import logging

logger = logging.getLogger(__name__)

@login_required(login_url='/login/')
def chat_view(request, conversation_id):
    conversation = get_object_or_404(Conversation, id=conversation_id)
    is_read_only = False

    is_deliberation = conversation.is_deliberation or hasattr(conversation, 'deliberation_dispute')
    if is_deliberation:
        dispute = getattr(conversation, 'deliberation_dispute', None)
        if not dispute:
            dispute = Dispute.objects.filter(deliberation_conversation=conversation).first()

        if not dispute:
            messages.error(request, "Dispute deliberation context not found.")
            return redirect('home')

        is_juror = dispute.jurors.filter(id=request.user.id).exists()
        if not is_juror and not request.user.is_staff:
            messages.error(request, "You are not authorized to access juror deliberation.")
            return redirect('home')

        if dispute.status != 'open':
            is_read_only = True
    else:
        if request.user in conversation.participants.all():
            is_read_only = False
        else:
            task = conversation.task
            is_juror = False
            if task and hasattr(task, 'dispute'):
                is_juror = task.dispute.jurors.filter(id=request.user.id).exists()

            if is_juror or request.user.is_staff:
                is_read_only = True
            else:
                messages.error(request, "You are not authorized to view this chat.")
                return redirect('home')

    messages_list = conversation.messages.all()

    try:
        notification_link = reverse('chat_view', args=[conversation_id])
        Notification.objects.filter(
            recipient=request.user, 
            link=notification_link, 
            is_read=False
        ).update(is_read=True)
    except Exception as e:
        logger.error(f"Error marking notifications: {e}")

    context = {
        'conversation': conversation,
        'messages': messages_list,
        'is_read_only': is_read_only
    }
    return render(request, 'chat.html', context)


@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)

        is_deliberation = conversation.is_deliberation or hasattr(conversation, 'deliberation_dispute')
        dispute = None
        if is_deliberation:
            dispute = getattr(conversation, 'deliberation_dispute', None)
            if not dispute:
                dispute = Dispute.objects.filter(deliberation_conversation=conversation).first()

            if not dispute:
                return HttpResponseForbidden("Dispute deliberation context not found.")

            is_juror = dispute.jurors.filter(id=request.user.id).exists()
            if not is_juror and not request.user.is_staff:
                return HttpResponseForbidden("You are not authorized to send messages in this deliberation chat.")

            if dispute.status != 'open':
                return HttpResponseForbidden("Deliberation chat is read-only because the dispute is resolved or withdrawn.")
        else:
            if request.user not in conversation.participants.all():
                task = conversation.task
                if task and hasattr(task, 'dispute') and task.dispute.jurors.filter(id=request.user.id).exists():
                    return HttpResponseForbidden("Jurors have read-only access to task chat history.")
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

            recipients = set()
            if is_deliberation and dispute:
                recipients.update(dispute.jurors.exclude(id=request.user.id))
            else:
                recipients.update(conversation.participants.exclude(id=request.user.id))

            for recipient in recipients:
                Notification.objects.create(
                    recipient=recipient,
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
