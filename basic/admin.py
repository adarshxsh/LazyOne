from django.contrib import admin
from .models import UserProfile, Task, Dispute, JuryAssignment, Friendship, FriendRequest, RewardLedger, Notification

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'rewards', 'college', 'is_phone_verified')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'posted_by', 'taken_by', 'status', 'reward')

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'is_escalated_to_staff', 'created_at')

@admin.register(JuryAssignment)
class JuryAssignmentAdmin(admin.ModelAdmin):
    list_display = ('dispute', 'juror', 'assigned_at', 'status')

admin.site.register(Friendship)
admin.site.register(FriendRequest)
admin.site.register(RewardLedger)
admin.site.register(Notification)

