from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(RewardLedger)
admin.site.register(Dispute)
