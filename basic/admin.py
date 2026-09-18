from django.contrib import admin
from .models import UserProfile, Task, Dispute, RewardLedger, JuryPool, JuryVote

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(RewardLedger)
admin.site.register(JuryPool)
admin.site.register(JuryVote)

