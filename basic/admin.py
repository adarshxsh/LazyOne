from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute, DisputeEvidence, FriendRequest, Friendship, Conversation, Message, Notification

# Register your models here.
admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(RewardLedger)
admin.site.register(Dispute)
admin.site.register(DisputeEvidence)
admin.site.register(FriendRequest)
admin.site.register(Friendship)
admin.site.register(Conversation)
admin.site.register(Message)
admin.site.register(Notification)

