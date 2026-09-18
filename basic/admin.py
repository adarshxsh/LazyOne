from django.contrib import admin
from .models import Dispute, DisputeEvidence, Task, UserProfile, RewardLedger

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(DisputeEvidence)
admin.site.register(RewardLedger)
