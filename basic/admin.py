from django.contrib import admin
from .models import Dispute, DisputeVote, Task, RewardLedger

admin.site.register(Dispute)
admin.site.register(DisputeVote)
admin.site.register(Task)
admin.site.register(RewardLedger)

