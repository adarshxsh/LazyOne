from django.shortcuts import render
from django.db.models import Q
from ..models import UserProfile, Task, Friendship, Conversation
import json

def home(request):
    # --- Search Logic ---
    query = request.GET.get('q', '')
    available_tasks = Task.objects.filter(is_taken=False, is_completed=False)
    if query:
        available_tasks = available_tasks.filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
    available_tasks = available_tasks.order_by('-created_at')[:20]

    # --- All non-completed tasks for the new section ---
    recent_tasks = Task.objects.filter(is_completed=False).order_by('-created_at')
    
    # Initialize context for anonymous users
    context = {
        'available_tasks': available_tasks,
        'recent_tasks': recent_tasks,
        'search_query': query,
        'user_nodes_json': json.dumps([]),
        'recent_conversations': []
    }

    if request.user.is_authenticated:
        # CRITICAL FIX: Ensure a user profile exists for every logged-in user.
        user_profile, created = UserProfile.objects.get_or_create(user=request.user)

        # --- Data for Recent Conversations ---
        context['recent_conversations'] = Conversation.objects.filter(participants=request.user).order_by('-task__created_at')[:10]

        # --- Data for Friend Circle Visualization ---
        user_nodes = []
        friendships = Friendship.objects.filter(from_user=user_profile)
        friend_ids = [friendship.to_user.user.id for friendship in friendships]
        
        for friendship in friendships:
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
        context['user_nodes_json'] = json.dumps(user_nodes)

        # Efficiently fetch conversation IDs for taken tasks
        taken_task_ids = [task.id for task in recent_tasks if task.is_taken]
        if taken_task_ids:
            conversations = Conversation.objects.filter(task_id__in=taken_task_ids)
            conversation_map = {conv.task_id: conv.id for conv in conversations}
            for task in recent_tasks:
                task.conversation_id = conversation_map.get(task.id)

    return render(request, 'home.html', context)
