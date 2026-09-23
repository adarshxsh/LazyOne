from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute, JuryAssignment, DisputeVote, Notification

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(RewardLedger)
admin.site.register(Dispute)
admin.site.register(JuryAssignment)
admin.site.register(DisputeVote)
admin.site.register(Notification)

