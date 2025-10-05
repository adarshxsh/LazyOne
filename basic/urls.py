from django.contrib import admin
from django.urls import path,include
from . import views
urlpatterns = [

    path('',views.home,name='home'),
    path('login/', views.login_page, name='login_page'),
    path('logout/', views.logout_view, name='logout'),
    path('firebase-login/', views.firebase_login, name='firebase_login'),
    path('profile/', views.profile_view, name='profile'),
    path('verify-phone-token/', views.verify_phone_token, name='verify_phone_token'),
    path('add_task/', views.add_task, name='add_task'),
    path('task/take/<int:task_id>/', views.take_task, name='take_task'),
    path('task/complete/<int:task_id>/', views.complete_task, name='complete_task'),
    path('my_tasks/', views.my_tasks, name='my_tasks'),
    path('users/', views.user_list, name='user_list'),
    path('friends/', views.friends_view, name='friends'),
    path('friend/send/<int:user_id>/', views.send_friend_request, name='send_friend_request'),
    path('friend/accept/<int:request_id>/', views.accept_friend_request, name='accept_friend_request'),
    path('friend/decline/<int:request_id>/', views.decline_friend_request, name='decline_friend_request'),
]
