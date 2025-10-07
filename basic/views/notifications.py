from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from ..models import Notification

@login_required(login_url='/login/')
def notifications_view(request):
    notifications = Notification.objects.filter(recipient=request.user).order_by('-created_at')
    # Mark all unread notifications as read when the page is viewed
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return render(request, 'notifications.html', {'notifications': notifications})
