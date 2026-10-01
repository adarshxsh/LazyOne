from django.contrib import admin
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(DisputeVote)
admin.site.register(RewardLedger)
