from django.contrib import admin
from django.urls import path, include
from .views import (
    home,
    logout_view,
    profile_view,
    user_profile_view,
    verify_phone_token,
    add_task,
    take_task,
    complete_task,
    my_tasks,
    start_chat,
    chat_view,
    send_message,
    user_list,
    friends_view,
    send_friend_request,
    accept_friend_request,
    decline_friend_request,
    notifications_view,
    rewards_view,
    cancel_task,
    request_cancellation,  # New
    accept_cancellation,  # New
    abandon_task,
    raise_dispute,
    dispute_detail_view,
    withdraw_dispute,
)

urlpatterns = [
    path('', home, name='home'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('user/<int:user_id>/', user_profile_view, name='user_profile'),
    path('verify-phone-token/', verify_phone_token, name='verify_phone_token'),
    path('rewards/', rewards_view, name='rewards'),

    # Task Lifecycle URLs
    path('add_task/', add_task, name='add_task'),
    path('task/take/<str:public_id>/', take_task, name='take_task'),
    path('task/complete/<str:public_id>/', complete_task, name='complete_task'),
    path('task/cancel/<str:public_id>/', cancel_task, name='cancel_task'),
    path('task/cancel/request/<str:public_id>/', request_cancellation, name='request_cancellation'),
    path('task/cancel/accept/<str:public_id>/', accept_cancellation, name='accept_cancellation'),
    path('task/abandon/<str:public_id>/', abandon_task, name='abandon_task'),
    path('task/dispute/<str:public_id>/', raise_dispute, name='raise_dispute'),
    path('dispute/<str:public_id>/', dispute_detail_view, name='dispute_detail'),
    path('dispute/withdraw/<str:public_id>/', withdraw_dispute, name='withdraw_dispute'),
    path('my_tasks/', my_tasks, name='my_tasks'),

    # Chat URLs
    path('chat/start/<int:user_id>/', start_chat, name='start_chat'),
    path('chat/<str:public_id>/', chat_view, name='chat_view'),
    path('chat/send/<str:public_id>/', send_message, name='send_message'),

    # User & Friend URLs
    path('users/', user_list, name='user_list'),
    path('friends/', friends_view, name='friends'),
    path('friend/send/<int:user_id>/', send_friend_request, name='send_friend_request'),
    path('friend/accept/<int:request_id>/', accept_friend_request, name='accept_friend_request'),
    path('friend/decline/<int:request_id>/', decline_friend_request, name='decline_friend_request'),

    # Notifications
    path('notifications/', notifications_view, name='notifications'),
]
