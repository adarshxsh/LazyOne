from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute, FriendRequest, Friendship, Conversation, Message, Notification


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'escrow_status', 'deposit_amount', 'created_at')
    list_filter = ('status', 'escrow_status', 'created_at')
    search_fields = ('task__title', 'raised_by__username', 'reason')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'reward', 'posted_by', 'taken_by', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'rewards', 'batch', 'college', 'is_phone_verified', 'is_instagram_verified')
    search_fields = ('user__username', 'user__email', 'first_name', 'last_name')


@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'task', 'amount', 'transaction_type', 'description', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'description', 'task__title')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipient', 'message', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('recipient__username', 'message')


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'last_message_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'sender', 'timestamp', 'is_read')
