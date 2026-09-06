from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, FriendRequest, Friendship, Notification
from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
import json

@login_required(login_url='/login/')
def user_list(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)

    # Get Firebase UIDs of users to exclude (friends, pending requests, self)
    friend_uids = list(user_profile.friends.all().values_list('firebase_uid', flat=True))
    sent_request_uids = list(FriendRequest.objects.filter(from_user=request.user).values_list('to_user__userprofile__firebase_uid', flat=True))
    received_request_uids = list(FriendRequest.objects.filter(to_user=request.user).values_list('from_user__userprofile__firebase_uid', flat=True))

    # Combine all UIDs to exclude, including the current user's
    exclude_uids = set(friend_uids) | set(sent_request_uids) | set(received_request_uids)
    if user_profile.firebase_uid:
        exclude_uids.add(user_profile.firebase_uid)

    context = {
        'exclude_uids_json': json.dumps(list(exclude_uids))
    }
    return render(request, 'user_list.html', context)

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
def send_friend_request(request, user_id):
    if request.method == 'POST':
        to_user = get_object_or_404(User, id=user_id)
        with transaction.atomic():
            friend_request, created = FriendRequest.objects.get_or_create(
                from_user=request.user,
                to_user=to_user
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
def accept_friend_request(request, request_id):
    with transaction.atomic():
        friend_request = FriendRequest.objects.select_for_update().filter(id=request_id).first()
        if not friend_request or friend_request.to_user != request.user:
            messages.error(request, 'Invalid request.')
            return redirect('friends')

        # To prevent deadlocks when two users accept mutual friend requests concurrently,
        # lock profiles in deterministic order by primary key 'id'.
        from_user_profile = UserProfile.objects.get(user=friend_request.from_user)
        to_user_profile = UserProfile.objects.get(user=request.user)

        locked_profiles = list(
            UserProfile.objects.select_for_update()
            .filter(id__in=[from_user_profile.id, to_user_profile.id])
            .order_by('id')
        )
        profile_map = {p.id: p for p in locked_profiles}
        locked_from_profile = profile_map[from_user_profile.id]
        locked_to_profile = profile_map[to_user_profile.id]

        locked_from_profile.friends.add(locked_to_profile)
        locked_to_profile.friends.add(locked_from_profile)
        closeness = request.POST.get('closeness', 50) if request.method == 'POST' else 50
        Friendship.objects.get_or_create(
            from_user=locked_from_profile,
            to_user=locked_to_profile,
            defaults={'closeness': closeness}
        )
        Friendship.objects.get_or_create(
            from_user=locked_to_profile,
            to_user=locked_from_profile,
            defaults={'closeness': closeness}
        )
        friend_request.delete()
        messages.success(request, 'Friend request accepted.')
        # Create notification for the sender
        Notification.objects.create(
            recipient=locked_from_profile.user,
            message=f"{request.user.username} accepted your friend request.",
            link=reverse('friends') # Link to the friends page
        )
    return redirect('friends')

@login_required(login_url='/login/')
def decline_friend_request(request, request_id):
    with transaction.atomic():
        friend_request = FriendRequest.objects.select_for_update().filter(id=request_id).first()
        if friend_request and friend_request.to_user == request.user:
            friend_request.delete()
            messages.success(request, 'Friend request declined.')
        else:
            messages.error(request, 'Invalid request.')
    return redirect('friends')
