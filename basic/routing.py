from django.urls import path, re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^ws/dispute/(?P<dispute_id>\d+)/$', consumers.DisputeConsumer.as_asgi()),
    re_path(r'^ws/user/$', consumers.UserConsumer.as_asgi()),
    path('ws/dispute/<int:dispute_id>/', consumers.DisputeConsumer.as_asgi()),
    path('ws/user/', consumers.UserConsumer.as_asgi()),
]
