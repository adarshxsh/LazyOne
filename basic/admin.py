from django.contrib import admin
from .models import UserProfile, Task, Dispute, JurorAssignment, Friendship, Notification

admin.site.register(UserProfile)
admin.site.register(Task)
admin.site.register(Dispute)
admin.site.register(JurorAssignment)
admin.site.register(Friendship)
admin.site.register(Notification)

