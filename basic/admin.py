from django.contrib import admin
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger, Notification, Conversation, Message, FriendRequest, Friendship

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(DisputeVote)
admin.site.register(RewardLedger)
admin.site.register(Notification)
admin.site.register(Conversation)
admin.site.register(Message)
admin.site.register(FriendRequest)
admin.site.register(Friendship)
