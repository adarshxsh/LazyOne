from django.contrib import admin
from .models import Dispute, DisputeEvidence, Task, UserProfile, RewardLedger, Notification

admin.site.register(Dispute)
admin.site.register(DisputeEvidence)
admin.site.register(Task)
admin.site.register(UserProfile)
admin.site.register(RewardLedger)
admin.site.register(Notification)
