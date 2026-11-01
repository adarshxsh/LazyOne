from django.shortcuts import render
from django.db.models import Q
from ..models import UserProfile, Task, Friendship, Conversation, Dispute
from ..services.feed import feed_for, public_feed
import json

def home(request):
    query = request.GET.get('q', '')
    if request.user.is_authenticated:
        # Wave-filtered feed: closest friends see a task first (ARCHITECTURE_V2 §4.3).
        available_tasks = feed_for(request.user, query, limit=20)
        disputed_tasks = (Task.objects.filter(disputes__status=Dispute.Status.OPEN,
                                              disputes__jurors__juror=request.user)
                          .select_related('posted_by', 'taken_by', 'conversation').distinct())
        recent_tasks = Task.objects.filter(Q(posted_by=request.user) | Q(taken_by=request.user),
                                           status__in=Task.ACTIVE_STATUSES).order_by('-created_at')
    else:
        available_tasks = public_feed(query)[:20]
        disputed_tasks = Task.objects.none()
        recent_tasks = Task.objects.none()

    # Initialize context for anonymous users
    context = {
        'disputed_tasks': disputed_tasks,
        'available_tasks': available_tasks,
        'recent_tasks': recent_tasks,
        'search_query': query,
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
