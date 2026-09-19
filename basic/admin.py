from django.contrib import admin
from .models import UserProfile, Task, Dispute, RewardLedger, Jury, JuryVote

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(RewardLedger)
admin.site.register(Jury)
admin.site.register(JuryVote)

