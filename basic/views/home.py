from django.shortcuts import render
from django.db.models import Q
from ..models import UserProfile, Task, Friendship, Conversation
import json

def home(request):
    # --- Disputed Tasks ---
    disputed_tasks = Task.objects.filter(status='disputed').order_by('-created_at')

    # --- Search Logic for Available Tasks ---
    query = request.GET.get('q', '')
    reward_type_filter = request.GET.get('reward_type', '')
    
    available_tasks = Task.objects.filter(status='available')
    recent_tasks = Task.objects.exclude(status__in=['completed', 'cancelled']).order_by('-created_at')

    if query:
        available_tasks = available_tasks.filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
        recent_tasks = recent_tasks.filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
    
    if reward_type_filter in ['points', 'usd']:
        available_tasks = available_tasks.filter(reward_type=reward_type_filter)
        recent_tasks = recent_tasks.filter(reward_type=reward_type_filter)

    available_tasks = available_tasks.order_by('-created_at')[:20]

    # Initialize context for anonymous users
    context = {
        'disputed_tasks': disputed_tasks,
        'available_tasks': available_tasks,
        'recent_tasks': recent_tasks,
        'search_query': query,
        'reward_type_filter': reward_type_filter,
        'user_nodes_json': json.dumps([]),
        'recent_conversations': []
    }

    if request.user.is_authenticated:
        user_profile, created = UserProfile.objects.get_or_create(user=request.user)

        # --- Data for Recent Conversations ---
        context['recent_conversations'] = Conversation.objects.filter(participants=request.user).order_by('-last_message_at')[:10]

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

    return render(request, 'home.html', context)
