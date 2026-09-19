from django.contrib import admin
from .models import Dispute, Task, RewardLedger, UserProfile

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'deposit_amount', 'escrow_status', 'created_at')
    list_filter = ('status', 'escrow_status', 'created_at')
    search_fields = ('task__title', 'reason', 'raised_by__username')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'reward', 'posted_by', 'taken_by', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'task', 'amount', 'transaction_type', 'description', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'description', 'task__title')

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'rewards', 'college', 'hostel')
    search_fields = ('user__username', 'user__email', 'college')
