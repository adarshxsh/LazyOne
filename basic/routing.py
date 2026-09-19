from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/disputes/', consumers.DisputeConsumer.as_asgi()),
    path('ws/disputes/<int:dispute_id>/', consumers.DisputeConsumer.as_asgi()),
    path('ws/dispute/<int:dispute_id>/', consumers.DisputeConsumer.as_asgi()),
]

