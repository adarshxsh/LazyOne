from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .models import UserProfile, Task, FriendRequest, Friendship, Conversation, Notification
from django.contrib.auth.models import User
from django.contrib.auth import login, logout, authenticate
from django.db import transaction
from django.db.models import Q
import json
from django.http import JsonResponse, HttpResponseForbidden
from firebase_admin import auth
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import requests
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)

# Create your views here.

@login_required
def home(request):
    # --- Search Logic ---
    query = request.GET.get('q', '')
    tasks = Task.objects.filter(is_taken=False, is_completed=False)
    if query:
        tasks = tasks.filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
    tasks = tasks.order_by('-created_at')[:20] # Show up to 20 results

    # --- Data for Recent Conversations ---
    recent_conversations = Conversation.objects.filter(participants=request.user).order_by('-task__created_at')[:10]

    # --- Data for Friend Circle Visualization ---
    user_nodes = []
    user_profile = get_object_or_404(UserProfile, user=request.user)
    friendships = Friendship.objects.filter(from_user=user_profile)
    friend_ids = []
    for friendship in friendships:
        friend_ids.append(friendship.to_user.user.id)
        user_nodes.append({
            'id': friendship.to_user.user.id,
            'username': friendship.to_user.user.username,
            'closeness': friendship.closeness,
            'phone': friendship.to_user.phone_number,
            'is_phone_verified': friendship.to_user.is_phone_verified,
            'instagram': friendship.to_user.instagram_username
        })
    max_nodes = 100
    if len(user_nodes) < max_nodes:
        exclude_ids = friend_ids + [request.user.id]
        other_users = UserProfile.objects.exclude(user__id__in=exclude_ids)[:max_nodes - len(user_nodes)]
        for other_user in other_users:
            user_nodes.append({
                'id': other_user.user.id,
                'username': other_user.user.username,
                'closeness': 0,
                'phone': other_user.phone_number,
                'is_phone_verified': other_user.is_phone_verified,
                'instagram': other_user.instagram_username
            })
    
    context = {
        'recent_conversations': recent_conversations,
        'tasks': tasks,
        'user_nodes_json': json.dumps(user_nodes),
        'search_query': query
    }
    return render(request, 'home.html', context)

@csrf_exempt
def firebase_login(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')
            if not id_token:
                return JsonResponse({'status': 'error', 'message': 'Token not provided'}, status=400)

            user = authenticate(request, token=id_token)

            if user is not None:
                login(request, user)
                return JsonResponse({'status': 'success', 'message': 'User logged in successfully'})
            else:
                return JsonResponse({'status': 'error', 'message': 'Invalid token or user not found.'}, status=401)

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)

def login_page(request):
    return render(request, 'login.html')

def logout_view(request):
    logout(request)
    return redirect('login_page')

@login_required
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        profile.first_name = request.POST.get('first_name', '')
        profile.last_name = request.POST.get('last_name', '')
        profile.bio = request.POST.get('bio', '')
        profile.college = request.POST.get('college', '')
        profile.major = request.POST.get('major', '')
        profile.roll_no = request.POST.get('roll_no', '')
        profile.batch = request.POST.get('batch', 2029)
        profile.phone_number = request.POST.get('phone_number', '')
        profile.instagram_username = request.POST.get('instagram_username', '')
        profile.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')
    return render(request, 'profile.html', {'profile': profile})

@login_required
def user_profile_view(request, user_id):
    viewed_user = get_object_or_404(User, id=user_id)
    viewed_profile = get_object_or_404(UserProfile, user=viewed_user)
    
    posted_tasks = Task.objects.filter(posted_by=viewed_user).order_by('-created_at')
    user_friends = viewed_profile.friends.all()

    context = {
        'viewed_profile': viewed_profile,
        'posted_tasks': posted_tasks,
        'user_friends': user_friends
    }
    return render(request, 'user_profile.html', context)

@login_required
def verify_phone_token(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')

            if not id_token:
                return JsonResponse({'success': False, 'error': 'No token provided.'}, status=400)

            decoded_token = auth.verify_id_token(id_token)
            firebase_phone_number = decoded_token.get('phone_number')

            user_profile = request.user.userprofile

            if user_profile.phone_number == firebase_phone_number:
                user_profile.is_phone_verified = True
                user_profile.save()
                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': 'Phone number mismatch.'}, status=400)

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@login_required
def chat_view(request, conversation_id):
    conversation = get_object_or_404(Conversation, id=conversation_id)
    if request.user not in conversation.participants.all():
        return HttpResponseForbidden("You are not authorized to view this chat.")
    return render(request, 'chat.html', {'conversation': conversation})

@login_required
def add_task(request):
    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        reward_str = request.POST.get('reward')

        try:
            reward = int(reward_str)
            if reward <= 0:
                messages.error(request, "Reward must be a positive number.")
                return render(request, 'add_task.html')

            user_profile = request.user.userprofile
            if user_profile.rewards < reward:
                messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                return render(request, 'add_task.html')

            with transaction.atomic():
                user_profile.rewards -= reward
                user_profile.save()

                Task.objects.create(
                    title=title,
                    description=description,
                    reward=reward,
                    posted_by=request.user
                )
            messages.success(request, f'Task added successfully! {reward} points have been reserved.')
            return redirect('home')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid reward amount.')
            return render(request, 'add_task.html')

    return render(request, 'add_task.html')

@login_required
def take_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if task.posted_by == request.user:
        messages.error(request, "You cannot take your own task.")
    elif task.is_taken:
        messages.error(request, "This task has already been taken.")
    else:
        with transaction.atomic():
            task.is_taken = True
            task.taken_by = request.user
            task.save()

            conversation, created = Conversation.objects.get_or_create(task=task)
            if created:
                conversation.participants.add(task.posted_by, task.taken_by)
                # Notify the task poster that their task has been taken
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"{request.user.username} has taken your task: {task.title}",
                    link=f"{{% url 'my_tasks' %}}" # Link to my tasks page
                )
                messages.success(request, "Task has been assigned to you and a chat has been created.")
            else:
                messages.success(request, "Task has been assigned to you.")

    return redirect('my_tasks')

@login_required
def complete_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if task.posted_by != request.user:
        messages.error(request, "You are not authorized to complete this task.")
    elif not task.is_taken:
        messages.error(request, "This task has not been taken by anyone yet.")
    elif task.is_completed:
        messages.error(request, "This task has already been completed.")
    else:
        with transaction.atomic():
            task_doer_profile = task.taken_by.userprofile
            task_doer_profile.rewards += task.reward
            task_doer_profile.save()

            task.is_completed = True
            task.save()
            messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')

@login_required
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    return render(request, 'my_tasks.html', {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks})

@login_required
def user_list(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)
    current_friends = user_profile.friends.all().values_list('user__id', flat=True)
    sent_requests = FriendRequest.objects.filter(from_user=request.user).values_list('to_user_id', flat=True)
    received_requests = FriendRequest.objects.filter(to_user=request.user).values_list('from_user_id', flat=True)
    exclude_ids = set(current_friends) | set(sent_requests) | set(received_requests)
    exclude_ids.add(request.user.id)
    users = UserProfile.objects.exclude(user__id__in=exclude_ids)
    return render(request, 'user_list.html', {'users': users})

@login_required
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

@login_required
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
                link=f"{{% url 'friends' %}}" # Link to the friends page
            )
        else:
            messages.info(request, 'Friend request already sent.')
    return redirect('friends')

@login_required
def accept_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        from_user_profile = UserProfile.objects.get(user=friend_request.from_user)
        to_user_profile = UserProfile.objects.get(user=request.user)
        from_user_profile.friends.add(to_user_profile)
        to_user_profile.friends.add(from_user_profile)
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
            link=f"{{% url 'friends' %}}" # Link to the friends page
        )
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')

@login_required
def decline_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        friend_request.delete()
        messages.success(request, 'Friend request declined.')
        # Optionally, notify the sender that their request was declined
        # Notification.objects.create(
        #     recipient=friend_request.from_user,
        #     message=f"{request.user.username} declined your friend request.",
        #     link=f"{{% url 'friends' %}}"
        # )
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')

@login_required
def notifications_view(request):
    notifications = Notification.objects.filter(recipient=request.user).order_by('-created_at')
    # Mark all unread notifications as read when the page is viewed
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return render(request, 'notifications.html', {'notifications': notifications})
