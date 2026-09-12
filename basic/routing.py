from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/dispute/<int:dispute_id>/', consumers.DisputeConsumer.as_asgi()),
    path('ws/notifications/', consumers.DisputeConsumer.as_asgi()),
]
