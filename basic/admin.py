from django.contrib import admin
from .models import (
    UserProfile, Task, RewardLedger, Dispute,
    FriendRequest, Friendship, Conversation, Message, Notification
)

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'rewards', 'phone_number', 'is_phone_verified', 'is_instagram_verified')
    search_fields = ('user__username', 'user__email', 'first_name', 'last_name', 'phone_number')
    list_filter = ('is_phone_verified', 'is_instagram_verified', 'batch')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'posted_by', 'taken_by', 'reward', 'status', 'created_at', 'deadline')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('user', 'task', 'amount', 'transaction_type', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'description', 'task__title')

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('task__title', 'raised_by__username', 'reason')

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'message', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('recipient__username', 'message')

@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'last_message_at')
    search_fields = ('task__title', 'participants__username')

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('sender', 'conversation', 'timestamp', 'is_read')
    search_fields = ('sender__username', 'content')

@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ('from_user', 'to_user', 'is_accepted', 'created_at')

@admin.register(Friendship)
class FriendshipAdmin(admin.ModelAdmin):
    list_display = ('from_user', 'to_user', 'closeness')

