from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'rewards', 'college', 'hostel')
    search_fields = ('user__username', 'first_name', 'last_name', 'college')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'posted_by', 'taken_by', 'reward', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('user', 'task', 'amount', 'transaction_type', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'description')

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('task', 'raised_by', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('task__title', 'raised_by__username', 'reason')

