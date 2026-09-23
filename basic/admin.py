from django.contrib import admin
from .models import UserProfile, Task, Dispute, DisputeJuror, Conversation, Message, RewardLedger

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(DisputeJuror)
admin.site.register(Conversation)
admin.site.register(Message)
admin.site.register(RewardLedger)
