from django.urls import path

from . import views

app_name = 'api'

urlpatterns = [
    path('me/', views.me, name='me'),
    path('tasks/', views.create_task, name='task_create'),
    path('tasks/feed/', views.task_feed, name='task_feed'),
    path('tasks/mine/', views.my_tasks, name='my_tasks'),
    path('tasks/<str:public_id>/', views.task_detail, name='task_detail'),
    path('tasks/<str:public_id>/events/', views.task_events, name='task_events'),
    path('tasks/<str:public_id>/disputes/', views.raise_dispute, name='raise_dispute'),
    path('tasks/<str:public_id>/<str:action>/', views.task_action, name='task_action'),
    path('disputes/<str:public_id>/', views.dispute_detail, name='dispute_detail'),
    path('disputes/<str:public_id>/<str:action>/', views.dispute_action, name='dispute_action'),
    path('conversations/<str:public_id>/messages/', views.conversation_messages, name='messages'),
    path('realtime/token/', views.realtime_token, name='realtime_token'),
    path('ledger/', views.ledger_history, name='ledger'),
]
