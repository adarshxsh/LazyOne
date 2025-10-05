from django.contrib import admin
from django.urls import path,include
from . import views
urlpatterns = [

    path('',views.home,name='home'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('profile/', views.profile_view, name='profile'),
    path('add_task/', views.add_task, name='add_task'),
    path('task/take/<int:task_id>/', views.take_task, name='take_task'),
    path('users/', views.user_list, name='user_list'),
    path('friends/', views.friends_view, name='friends'),
    path('friend/send/<int:user_id>/', views.send_friend_request, name='send_friend_request'),
    path('friend/accept/<int:request_id>/', views.accept_friend_request, name='accept_friend_request'),
    path('friend/decline/<int:request_id>/', views.decline_friend_request, name='decline_friend_request'),
]
