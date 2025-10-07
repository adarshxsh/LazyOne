from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from ..models import Conversation, Message, Notification
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils import timezone

@login_required(login_url='/login/')
def chat_view(request, conversation_id):
    conversation = get_object_or_404(Conversation, id=conversation_id)
    if request.user not in conversation.participants.all():
        return HttpResponseForbidden("You are not authorized to view this chat.")

    # Mark messages as read
    unread_messages = conversation.messages.filter(is_read=False).exclude(sender=request.user)
    for message in unread_messages:
        message.is_read = True
        message.save()

    # Mark related notifications as read
    Notification.objects.filter(
        recipient=request.user, 
        link=reverse('chat_view', args=[conversation_id]), 
        is_read=False
    ).update(is_read=True)

    messages = conversation.messages.all()
    return render(request, 'chat.html', {'conversation': conversation, 'messages': messages})

@login_required(login_url='/login/')
def send_message(request, conversation_id):
    if request.method == 'POST':
        conversation = get_object_or_404(Conversation, id=conversation_id)
        if request.user not in conversation.participants.all():
            return HttpResponseForbidden("You are not authorized to send messages in this chat.")
        
        content = request.POST.get('content')
        if content:
            new_message = Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=content
            )

            # Update the conversation's last_message_at timestamp
            conversation.last_message_at = timezone.now()
            conversation.save()

            # Notify other participants
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
