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
        # Contact details (phone, Instagram) go only to MUTUAL friends. Other people in the
        # community appear with username only, never contact details (SECURITY.md #3).
        user_nodes = []
        friendships = list(Friendship.objects.filter(from_user=user_profile).select_related('to_user__user'))
        friend_pids = [f.to_user_id for f in friendships]
        mutual = set(Friendship.objects.filter(from_user_id__in=friend_pids, to_user=user_profile)
                     .values_list('from_user_id', flat=True))
        for friendship in friendships:
            friend = friendship.to_user
            share = friend.pk in mutual
            user_nodes.append({
                'id': friend.user.id,
                'username': friend.user.username,
                'closeness': friendship.closeness,
                'is_friend': True,
                'phone': friend.phone_number if share else None,
                'is_phone_verified': friend.is_phone_verified if share else False,
                'instagram': friend.instagram_username if share else None,
            })

        max_nodes = 100
        if len(user_nodes) < max_nodes:
            others = (UserProfile.objects.filter(community=user_profile.get_community(), user__is_active=True)
                      .exclude(pk__in=friend_pids + [user_profile.pk]).select_related('user')
                      [:max_nodes - len(user_nodes)])
            for other in others:
                user_nodes.append({
                    'id': other.user.id,
                    'username': other.user.username,
                    'closeness': 0,
                    'is_friend': False,
                    'phone': None,
                    'is_phone_verified': False,
                    'instagram': None,
                })
        context['user_nodes_json'] = json.dumps(user_nodes)

    return render(request, 'home.html', context)
