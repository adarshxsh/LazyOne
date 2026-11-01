from django.views.decorators.http import require_POST
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, FriendRequest, Friendship, Notification
from django.contrib.auth.models import User
from django.urls import reverse
import json
from ..ratelimit import ratelimit

@login_required(login_url='/login/')
def user_list(request):
    """
    People in the viewer's community they aren't connected to yet. Server-rendered from
    Postgres: only public fields (username, name, tier); never phone, Instagram or roll no.
    (The old page read the whole Firestore `users` collection in the browser.)
    """
    from ..services import prestige
    user_profile = get_object_or_404(UserProfile, user=request.user)
    query = request.GET.get('q', '').strip()[:50]

    exclude_ids = set(user_profile.friends.values_list('user_id', flat=True))
    exclude_ids |= set(FriendRequest.objects.filter(from_user=request.user).values_list('to_user_id', flat=True))
    exclude_ids |= set(FriendRequest.objects.filter(to_user=request.user).values_list('from_user_id', flat=True))
    exclude_ids.add(request.user.id)

    people = (UserProfile.objects.filter(community=user_profile.get_community(), user__is_active=True)
              .exclude(user_id__in=exclude_ids).select_related('user'))
    if query:
        people = people.filter(user__username__icontains=query)
    people = list(people.order_by('-prestige', 'user__username')[:50])
    for p in people:
        p.tier = prestige.tier(p.prestige)
    return render(request, 'user_list.html', {'people': people, 'search_query': query})

@login_required(login_url='/login/')
def friends_view(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)
    friendships = Friendship.objects.filter(from_user=user_profile).order_by('-closeness')
    friend_requests = FriendRequest.objects.filter(to_user=request.user, is_accepted=False)
    current_friends = user_profile.friends.all().values_list('user__id', flat=True)
    sent_requests = FriendRequest.objects.filter(from_user=request.user).values_list('to_user_id', flat=True)
    received_requests = FriendRequest.objects.filter(to_user=request.user).values_list('from_user_id', flat=True)
    exclude_ids = set(current_friends) | set(sent_requests) | set(received_requests)
    exclude_ids.add(request.user.id)
    other_users = UserProfile.objects.exclude(user__id__in=exclude_ids)
    context = {
        'friendships': friendships,
        'friend_requests': friend_requests,
        'other_users': other_users
    }
    return render(request, 'friends.html', context)

@login_required(login_url='/login/')
@require_POST
@ratelimit('friend_request', '30/hour')
def send_friend_request(request, user_id):
    if request.method == 'POST':
        to_user = get_object_or_404(User, id=user_id)
        closeness = request.POST.get('closeness', 50)
        friend_request, created = FriendRequest.objects.get_or_create(
            from_user=request.user,
            to_user=to_user,
            defaults={'closeness': closeness}
        )
        if created:
            messages.success(request, 'Friend request sent.')
            # Create notification for the recipient
            Notification.objects.create(
                recipient=to_user,
                message=f"{request.user.username} sent you a friend request.",
                link=reverse('friends') # Link to the friends page
            )
        else:
            messages.info(request, 'Friend request already sent.')
    return redirect('friends')

@login_required(login_url='/login/')
@require_POST
def accept_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        from_user_profile = UserProfile.objects.get(user=friend_request.from_user)
        to_user_profile = UserProfile.objects.get(user=request.user)
        Friendship.objects.get_or_create(
            from_user=from_user_profile,
            to_user=to_user_profile,
            defaults={'closeness': friend_request.closeness}
        )
        Friendship.objects.get_or_create(
            from_user=to_user_profile,
            to_user=from_user_profile,
            defaults={'closeness': friend_request.closeness}
        )
        friend_request.delete()
        messages.success(request, 'Friend request accepted.')
        # Create notification for the sender
        Notification.objects.create(
            recipient=from_user_profile.user,
            message=f"{request.user.username} accepted your friend request.",
            link=reverse('friends') # Link to the friends page
        )
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')

@login_required(login_url='/login/')
@require_POST
def decline_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        friend_request.delete()
        messages.success(request, 'Friend request declined.')
        # Optionally, notify the sender that their request was declined
        # Notification.objects.create(
        #     recipient=friend_request.from_user,
        #     message=f\"{request.user.username} declined your friend request.\",
        #     link=f\"{% url 'friends' %}\"
        # )
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')
