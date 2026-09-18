from django.urls import path
from .views.home import home
from .views.authentication import login_page, logout_view, register_view, verify_otp_view
from .views.profile import profile_view, user_profile_view, update_closeness
from .views.tasks import (
    add_task, take_task, complete_task, my_tasks, cancel_task, 
    request_cancellation, accept_cancellation, abandon_task
)
from .views.dispute import dispute_detail_view, withdraw_dispute, raise_dispute
from .views.chat import start_chat, chat_view, send_message
from .views.friends import friends_view, send_friend_request, accept_friend_request, decline_friend_request, user_list
from .views.notifications import notifications_view
from .views.rewards import rewards_view

urlpatterns = [
    path('', home, name='home'),
    path('login/', login_page, name='login_page'),
    path('register/', register_view, name='register'),
    path('verify-otp/', verify_otp_view, name='verify_otp'), # Add this line
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('user/<int:user_id>/', user_profile_view, name='user_profile'),
    path('rewards/', rewards_view, name='rewards'),

    # Task Lifecycle URLs
    path('add_task/', add_task, name='add_task'),
    path('task/take/<int:task_id>/', take_task, name='take_task'),
    path('task/complete/<int:task_id>/', complete_task, name='complete_task'),
    path('task/cancel/<int:task_id>/', cancel_task, name='cancel_task'),
    path('task/cancel/request/<int:task_id>/', request_cancellation, name='request_cancellation'),
    path('task/cancel/accept/<int:task_id>/', accept_cancellation, name='accept_cancellation'),
    path('task/abandon/<int:task_id>/', abandon_task, name='abandon_task'),
    path('my_tasks/', my_tasks, name='my_tasks'),

    # Dispute URLs
    path('task/dispute/<int:task_id>/', raise_dispute, name='raise_dispute'),
    path('dispute/<int:dispute_id>/', dispute_detail_view, name='dispute_detail'),
    path('dispute/withdraw/<int:dispute_id>/', withdraw_dispute, name='withdraw_dispute'),

    # Chat URLs
    path('chat/start/<int:user_id>/', start_chat, name='start_chat'),
    path('chat/<int:conversation_id>/', chat_view, name='chat_view'),
    path('chat/send/<int:conversation_id>/', send_message, name='send_message'),

    # User & Friend URLs
    path('users/', user_list, name='user_list'),
    path('friends/', friends_view, name='friends'),
    path('friend/send/<int:user_id>/', send_friend_request, name='send_friend_request'),
    path('friend/accept/<int:request_id>/', accept_friend_request, name='accept_friend_request'),
    path('friend/decline/<int:request_id>/', decline_friend_request, name='decline_friend_request'),
    path('friend/closeness/update/<int:friendship_id>/', update_closeness, name='update_closeness'),

    # Notifications
    path('notifications/', notifications_view, name='notifications'),
]
